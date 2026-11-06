from sqlalchemy import text

x = 1
name = "t"
conn.execute(text(f"SELECT * FROM t WHERE id = {x}"))
conn.execute(text("SELECT * FROM t WHERE id = %s" % x))
conn.execute(text("SELECT * FROM {}".format(name)))
cur.execute(f"SELECT * FROM t WHERE id = {x}")
cur.execute("SELECT * FROM t WHERE id = " + str(x))
