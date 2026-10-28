#!/usr/bin/env python3
"""Builds docs/catalog.json from base_catalog.json plus live rewards data.

For each source in sources.json it downloads the page, asks Claude to pull out the
rotating categories as structured data, checks the result, and merges it into the
card's rules. If anything looks wrong it exits with an error and publishes nothing,
so the last good catalog stays live.

Needs the ANTHROPIC_API_KEY environment variable. No third-party packages.
"""
import datetime
import json
import os
import re
import sys
import urllib.request
from html.parser import HTMLParser

ROOT = os.path.dirname(os.path.abspath(__file__))
API_URL = "https://api.anthropic.com/v1/messages"
MAX_PAGE_CHARS = 60000

# Quarter number -> (start month, end month, last day of end month)
QUARTER_DATES = {1: (1, 3, 31), 2: (4, 6, 30), 3: (7, 9, 30), 4: (10, 12, 31)}


class Failure(Exception):
    """Something is wrong enough that we should not publish."""


# ---------- fetching ----------

class _TextExtractor(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []
        self.skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "noscript", "svg"):
            self.skip += 1
        if tag in ("h1", "h2", "h3", "p", "li", "tr", "br"):
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style", "noscript", "svg") and self.skip:
            self.skip -= 1

    def handle_data(self, data):
        if not self.skip:
            self.parts.append(data)


def page_text(url):
    request = urllib.request.Request(url, headers={"User-Agent": "which-card-catalog-bot/1.0"})
    with urllib.request.urlopen(request, timeout=30) as response:
        html = response.read().decode("utf-8", errors="replace")
    extractor = _TextExtractor()
    extractor.feed(html)
    text = re.sub(r"[ \t]+", " ", "".join(extractor.parts))
    text = re.sub(r"\n\s*\n+", "\n", text).strip()
    # The FAQ repeats old information; drop it if the page has one.
    cut = text.find("Frequently Asked Questions")
    if cut > 0:
        text = text[:cut]
    return text[:MAX_PAGE_CHARS]


# ---------- asking Claude ----------

def extraction_tool(category_ids):
    return {
        "name": "record_rewards",
        "description": "Record the rotating bonus categories listed on the page.",
        "input_schema": {
            "type": "object",
            "properties": {
                "quarters": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "year": {"type": "integer"},
                            "quarter": {"type": "integer", "enum": [1, 2, 3, 4]},
                            "categories": {
                                "type": "array",
                                "items": {"type": "string", "enum": category_ids},
                                "description": "Our category ids that match the page's categories for this quarter.",
                            },
                            "unmapped": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "Page categories that fit none of our category ids, copied as written.",
                            },
                            "rate": {"type": "number", "description": "Bonus rate, e.g. 5 for 5%."},
                            "cap": {"type": "number", "description": "Spending cap in dollars, or 0 if none."},
                            "activation_required": {"type": "boolean"},
                        },
                        "required": ["year", "quarter", "categories", "unmapped", "rate", "cap", "activation_required"],
                    },
                }
            },
            "required": ["quarters"],
        },
    }


def ask_claude(model, text, categories):
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise Failure("ANTHROPIC_API_KEY is not set")

    category_lines = "\n".join("- %s: %s. %s" % (c["id"], c["name"], c.get("description") or "")
                               for c in categories)
    prompt = (
        "Below is a web page listing a credit card's rotating quarterly bonus categories.\n"
        "Extract every quarter in the page's bonus category calendar. Ignore FAQ answers, "
        "ads and anything that isn't the calendar.\n"
        "Map each page category to our category ids. If a page category fits none of them, "
        "list it under unmapped instead of forcing a match. Use only what the page says; "
        "do not guess rates, caps or dates.\n\n"
        "Our categories:\n%s\n\nPAGE:\n%s" % (category_lines, text)
    )
    tool = extraction_tool([c["id"] for c in categories])
    body = {
        "model": model,
        "max_tokens": 2000,
        "tools": [tool],
        "tool_choice": {"type": "tool", "name": tool["name"]},
        "messages": [{"role": "user", "content": prompt}],
    }
    request = urllib.request.Request(
        API_URL,
        data=json.dumps(body).encode("utf-8"),
        headers={"x-api-key": api_key, "anthropic-version": "2023-06-01",
                 "content-type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=120) as response:
        reply = json.load(response)
    for block in reply.get("content", []):
        if block.get("type") == "tool_use" and block.get("name") == tool["name"]:
            return block["input"]
    raise Failure("Claude did not return structured data")


# ---------- checking and merging ----------

def validate(extracted, category_ids, today):
    quarters = extracted.get("quarters")
    if not quarters:
        raise Failure("no quarters extracted")
    for q in quarters:
        label = "%s Q%s" % (q.get("year"), q.get("quarter"))
        if q["quarter"] not in QUARTER_DATES:
            raise Failure("%s: bad quarter" % label)
        if not (2000 <= q["year"] <= 2100):
            raise Failure("%s: implausible year" % label)
        if not (1 <= q["rate"] <= 10):
            raise Failure("%s: implausible rate %s" % (label, q["rate"]))
        if q["cap"] < 0 or q["cap"] > 100000:
            raise Failure("%s: implausible cap %s" % (label, q["cap"]))
        for cid in q["categories"]:
            if cid not in category_ids:
                raise Failure("%s: unknown category id %s" % (label, cid))
    # The calendar must cover today, otherwise the page is stale or we misread it.
    current = (today.year, (today.month - 1) // 3 + 1)
    current_q = next((q for q in quarters if (q["year"], q["quarter"]) == current), None)
    if current_q is None or not current_q["categories"]:
        raise Failure("no categories found for the current quarter %s Q%s" % current)


def rules_from(quarters):
    rules = []
    for q in sorted(quarters, key=lambda q: (q["year"], q["quarter"])):
        if not q["categories"]:
            continue
        first_month, last_month, last_day = QUARTER_DATES[q["quarter"]]
        rule = {
            "categories": q["categories"],
            "rate": q["rate"],
            "startDate": "%d-%02d-01" % (q["year"], first_month),
            "endDate": "%d-%02d-%02d" % (q["year"], last_month, last_day),
        }
        if q["cap"]:
            rule["cap"] = q["cap"]
        if q["activation_required"]:
            rule["activationRequired"] = True
        rules.append(rule)
    return rules


def main():
    today = datetime.date.today()
    with open(os.path.join(ROOT, "base_catalog.json")) as f:
        catalog = json.load(f)
    with open(os.path.join(ROOT, "sources.json")) as f:
        config = json.load(f)

    category_ids = [c["id"] for c in catalog["categories"]]
    cards = {c["id"]: c for c in catalog["cards"]}

    for source in config["sources"]:
        card = cards.get(source["cardId"])
        if card is None:
            raise Failure("source refers to unknown card %s" % source["cardId"])
        print("Fetching %s" % source["url"])
        text = page_text(source["url"])
        if len(text) < 500:
            raise Failure("page text suspiciously short for %s" % source["cardId"])
        extracted = ask_claude(config["model"], text, catalog["categories"])
        validate(extracted, category_ids, today)
        for q in extracted["quarters"]:
            if q["unmapped"]:
                print("NOTE %s %s Q%s unmapped categories: %s"
                      % (source["cardId"], q["year"], q["quarter"], q["unmapped"]))
        card["rules"] = card["rules"] + rules_from(extracted["quarters"])

    catalog["version"] = int(today.strftime("%Y%m%d"))
    out_dir = os.path.join(ROOT, "docs")
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, "catalog.json")

    # Don't rewrite the file (and create a pointless commit) if only the date changed.
    if os.path.exists(out_path):
        with open(out_path) as f:
            old = json.load(f)
        if old.get("cards") == catalog["cards"] and old.get("categories") == catalog["categories"]:
            print("No changes to publish.")
            return
        if len(catalog["cards"]) < len(old.get("cards", [])):
            raise Failure("new catalog has fewer cards than the published one")

    with open(out_path, "w") as f:
        json.dump(catalog, f, indent=2)
        f.write("\n")
    print("Wrote %s" % out_path)


if __name__ == "__main__":
    try:
        main()
    except Failure as error:
        print("FAILED: %s" % error)
        sys.exit(1)
