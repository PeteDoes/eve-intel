import os
import json
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
import httpx
import redis.asyncio as redis

app = FastAPI()

redis_client = redis.from_url(os.environ.get("REDIS_URL"), decode_responses=True)

@app.get("/")
def read_root():
    return FileResponse("static/index.html")

@app.get("/api/character/{name}")
async def get_character(name: str):
    cache_key = f"character:{name.lower()}"

    cached = await redis_client.get(cache_key)
    if cached:
        result = json.loads(cached)
        result["from_cache"] = True
        return result

    headers = {"User-Agent": "eve-intel-app (contact: your-email@example.com)"}
    async with httpx.AsyncClient(headers=headers, follow_redirects=True) as client:
        search_resp = await client.post(
            "https://esi.evetech.net/latest/universe/ids/",
            json=[name]
        )
        search_data = search_resp.json()

        if "characters" not in search_data or len(search_data["characters"]) == 0:
            return {"error": f"No character found with name '{name}'"}

        character_id = search_data["characters"][0]["id"]

        detail_resp = await client.get(
            f"https://esi.evetech.net/latest/characters/{character_id}/"
        )
        details = detail_resp.json()

        corporation_id = details.get("corporation_id")
        alliance_id = details.get("alliance_id")

        corp_name = None
        if corporation_id:
            corp_resp = await client.get(
                f"https://esi.evetech.net/latest/corporations/{corporation_id}/"
            )
            corp_name = corp_resp.json().get("name")

        alliance_name = None
        if alliance_id:
            alliance_resp = await client.get(
                f"https://esi.evetech.net/latest/alliances/{alliance_id}/"
            )
            alliance_name = alliance_resp.json().get("name")

        zkill_stats = {}
        zkill_debug_raw = None
        try:
            zkill_resp = await client.get(
                f"https://zkillboard.com/api/stats/characterID/{character_id}/"
            )
            zkill_data = zkill_resp.json()
            zkill_debug_raw = zkill_data.get("topLists")
            zkill_stats = {
                "ships_destroyed": zkill_data.get("shipsDestroyed"),
                "ships_lost": zkill_data.get("shipsLost"),
                "isk_destroyed": zkill_data.get("iskDestroyed"),
                "isk_lost": zkill_data.get("iskLost"),
                "danger_ratio": zkill_data.get("dangerRatio"),
                "gang_ratio": zkill_data.get("gangRatio"),
            }
        except Exception as e:
            zkill_stats = {"error": f"Could not fetch zKillboard stats: {str(e)}"}

        result = {
            "name": name,
            "character_id": character_id,
            "security_status": details.get("security_status"),
            "birthday": details.get("birthday"),
            "corporation_id": corporation_id,
            "corporation_name": corp_name,
            "alliance_id": alliance_id,
            "alliance_name": alliance_name,
            "zkillboard": zkill_stats,
            "debug_topLists": zkill_debug_raw,
            "from_cache": False,
        }

        await redis_client.set(cache_key, json.dumps(result), ex=600)

        return result

app.mount("/", StaticFiles(directory="static"), name="static")
