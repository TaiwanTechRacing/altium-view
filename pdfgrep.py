import fitz, sys, re, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
doc = fitz.open(sys.argv[1])
pat = re.compile(sys.argv[2], re.I)
ctx = int(sys.argv[3]) if len(sys.argv) > 3 else 2
for i, page in enumerate(doc):
    lines = page.get_text().replace('\xa0',' ').split("\n")
    hits = [j for j, l in enumerate(lines) if pat.search(l)]
    if hits:
        print("=== page %d ===" % (i+1))
        shown = set()
        for j in hits:
            for k in range(max(0, j-ctx), min(len(lines), j+ctx+1)):
                if k not in shown:
                    shown.add(k); print("  %s" % lines[k])
            print("  ---")
