import glob
import pandas as pd

consultant_leave_files = glob.glob('leave/*csv')

first = True
for f in consultant_leave_files :
    if first :
        df = pd.read_csv(f)
        first = False
    else :
        df = pd.concat([df, pd.read_csv(f)])

print(df.head())

