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

def extract_top_list(top_lists, list_type, limit=5):
    for entry in top_lists:
        if entry.get("type") == list_type:
            values = entry.get("values", [])
            return values[:limit]
    return []

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
        top_ships = []
        top_characters = []
        top_corporations = []
        top_alliances = []

        try:
            zkill_resp = await client.get(
                f"https://zkillboard.com/api/stats/characterID/{character_id}/"
            )
            zkill_data = zkill_resp.json()
            top_lists = zkill_data.get("topLists", [])

            zkill_stats = {
                "ships_destroyed": zkill_data.get("shipsDestroyed"),
                "ships_lost": zkill_data.get("shipsLost"),
                "isk_destroyed": zkill_data.get("iskDestroyed"),
                "isk_lost": zkill_data.get("iskLost"),
                "danger_ratio": zkill_data.get("dangerRatio"),
                "gang_ratio": zkill_data.get("gangRatio"),
            }

            # Preferred ships
            for ship in extract_top_list(top_lists, "shipType"):
                top_ships.append({
                    "name": ship.get("shipName"),
                    "kills": ship.get("kills"),
                })

            # Who they fly with
            for char in extract_top_list(top_lists, "character"):
                if char.get("characterID") != character_id:
                    top_characters.append({
                        "name": char.get("characterName"),
                        "kills": char.get("kills"),
                    })

            for corp in extract_top_list(top_lists, "corporation"):
                top_corporations.append({
                    "name": corp.get("corporationName"),
                    "kills": corp.get("kills"),
                })

            for alliance in extract_top_list(top_lists, "alliance"):
                top_alliances.append({
                    "name": alliance.get("allianceName"),
                    "kills": alliance.get("kills"),
                })

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
            "top_ships": top_ships,
            "flies_with_characters": top_characters,
            "flies_with_corporations": top_corporations,
            "flies_with_alliances": top_alliances,
            "from_cache": False,
        }

        await redis_client.set(cache_key, json.dumps(result), ex=600)

        return result

app.mount("/", StaticFiles(directory="static"), name="static")
