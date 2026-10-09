import sqlite3

con = sqlite3.connect("file:t_dubber.db?mode=ro", uri=True)
tabs = [r[0] for r in con.execute("select name from sqlite_master where type='table'")]
print("tables:", tabs)
for t in tabs:
    cols = [c[1] for c in con.execute("PRAGMA table_info(%s)" % t)]
    n = con.execute("select count(*) from %s" % t).fetchone()[0]
    print("-", t, n, cols)
