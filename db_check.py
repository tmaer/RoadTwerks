"""Quick Tiger Data connection test.  Run:  py -V:3.12 db_check.py"""
import asyncio, os, socket, time
import asyncpg
from dotenv import load_dotenv

load_dotenv()
host, port = os.getenv("DB_HOST"), int(os.getenv("DB_PORT", "30650"))

print(f"1) Network: can this PC reach {host}:{port} ?")
try:
    socket.create_connection((host, port), timeout=10).close()
    print("   OK - port reachable")
except Exception as e:
    print(f"   FAILED - {type(e).__name__}: {e}")
    print("   -> Your network is blocking Tiger (or no internet). Try your phone's hotspot.")
    raise SystemExit

async def main():
    t = time.time()
    conn = await asyncpg.connect(user=os.getenv("DB_USER"), password=os.getenv("DB_PASSWORD"),
                                 host=host, port=port, database=os.getenv("DB_NAME", "tsdb"),
                                 ssl=os.getenv("DB_SSL", "require"), timeout=20)
    n = await conn.fetchval("SELECT count(*) FROM public.detectors")
    await conn.close()
    print(f"   OK - logged in, {n} cameras found ({time.time() - t:.1f}s)")

print("2) Login + query")
try:
    asyncio.run(main())
except Exception as e:
    print(f"   FAILED - {type(e).__name__}: {e!r}")
    if "password" in str(e).lower():
        print("   -> Wrong password: update DB_PASSWORD in .env with the current Tiger password.")
    else:
        print("   -> Check in console.cloud.tigerdata.com that the service is Running (not Paused).")
