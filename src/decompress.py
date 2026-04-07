import zstandard as zstd
import os

input_file = r"C:\Users\kyawz\Downloads\GLBX-20260315-XECEHWSFA6\glbx-mdp3-20100606-20260314.ohlcv-1m.csv.zst"
output_file = r"C:\Users\kyawz\Downloads\GLBX-20260315-XECEHWSFA6\MES_1m_bars.csv"

print("Decompressing... this may take a minute...")

with open(input_file, 'rb') as compressed:
    dctx = zstd.ZstdDecompressor()
    with open(output_file, 'wb') as destination:
        dctx.copy_stream(compressed, destination)

print(f"Done! Output size: {os.path.getsize(output_file) / 1e6:.1f} MB")