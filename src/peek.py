import pandas as pd

df = pd.read_csv(r"C:\Users\kyawz\Downloads\GLBX-20260315-XECEHWSFA6\MES_1m_bars.csv", nrows=5)
print(df.columns.tolist())
print(df.head())