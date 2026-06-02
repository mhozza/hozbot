#!/usr/bin/env python3
"""Look up a UK postcode or address and return matching UPRNs.

Usage:
    uv run tools/get_uprn.py "AL1 5TE"
    uv run tools/get_uprn.py "30 Buttermere Close"
"""

import argparse
import xml.etree.ElementTree as ET
import requests

BASE = "https://gis.stalbans.gov.uk/NoticeBoard9"


def search(query: str) -> list[dict]:
    session = requests.Session()
    session.headers.update({"User-Agent": "Mozilla/5.0"})
    session.get(f"{BASE}/NoticeBoard.aspx", timeout=30)

    soap_req = f'''<?xml version="1.0" encoding="utf-8"?>
<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/">
  <soap:Body>
    <GetResults xmlns="http://tempuri.org/">
      <filter>{query}</filter>
      <startIndex>0</startIndex>
      <endIndex>30</endIndex>
    </GetResults>
  </soap:Body>
</soap:Envelope>'''

    r = session.post(
        f"{BASE}/quicksearch.asmx",
        data=soap_req,
        headers={
            "Content-Type": "text/xml; charset=utf-8",
            "SOAPAction": "http://tempuri.org/GetResults",
        },
        timeout=30,
    )

    ns = {"t": "http://tempuri.org/"}
    root = ET.fromstring(r.content)
    rows = root.findall(".//t:Row", ns)

    results = []
    for row in rows:
        cols = {
            c.find("t:Name", ns).text: c.find("t:Value", ns).text
            for c in row.findall("t:Columns/t:Column", ns)
            if c.find("t:Name", ns) is not None
        }
        results.append(cols)
    return results


def main():
    parser = argparse.ArgumentParser(description="Look up UPRN for a UK address")
    parser.add_argument("query", help="Postcode or partial address to search")
    args = parser.parse_args()

    results = search(args.query)
    if not results:
        print("No results found.")
        return

    print(f"{'Address':<65} {'UPRN':<15} {'Easting':<10} {'Northing':<10}")
    print("-" * 100)
    for r in results:
        print(
            f"{r.get('ADDRESS', '?'):<65} "
            f"{r.get('UPRN', '?'):<15} "
            f"{r.get('EASTING', '?'):<10} "
            f"{r.get('NORTHING', '?'):<10}"
        )


if __name__ == "__main__":
    main()
