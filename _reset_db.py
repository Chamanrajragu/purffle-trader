import sqlite3
c = sqlite3.connect(r"D:\cryptobot\cryptobot.db")
c.executescript("""
DELETE FROM trades;
DELETE FROM positions;
DELETE FROM snapshots;
UPDATE state SET value='100.0' WHERE key IN ('cash','starting_capital');
""")
c.commit()
print("DB reset: trades/positions/snapshots cleared, cash=$100, starting_capital=$100")
