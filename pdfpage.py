import fitz, sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
doc = fitz.open(sys.argv[1])
for spec in sys.argv[2].split(','):
    a,_,b = spec.partition('-')
    for p in range(int(a), int(b or a)+1):
        print("=== page %d ===" % p)
        print(doc[p-1].get_text().replace('\xa0',' '))
