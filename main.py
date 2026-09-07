import os
import json
from collections import Counter
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

async def get_fleetmates(client, character_id, kill_limit=25, name_limit=15):
    """Scan recent killmails to find who this character actually flies with."""
    try:
        kills_resp = await client.get(
            f"https://zkillboard.com/api/kills/characterID/{character_id}/"
        )
        kills_list = kills_resp.json()
    except Exception:
        return []

    if not isinstance(kills_list, list):
        return []

    kills_to_check = kills_list[:kill_limit]

    fleetmate_counter = Counter()

    for kill in kills_to_check:
        killmail_id = kill.get("killmail_id")
        kill_hash = kill.get("zkb", {}).get("hash")

        if not killmail_id or not kill_hash:
            continue

        try:
            killmail_resp = await client.get(
                f"https://esi.evetech.net/latest/killmails/{killmail_id}/{kill_hash}/"
            )
            killmail_data = killmail_resp.json()
        except Exception:
            continue

        attackers = killmail_data.get("attackers", [])
        for attacker in attackers:
            attacker_id = attacker.get("character_id")
            if attacker_id and attacker_id != character_id:
                fleetmate_counter[attacker_id] += 1

    if not fleetmate_counter:
        return []

    top_fleetmate_ids = [char_id for char_id, count in fleetmate_counter.most_common(name_limit)]

    # Resolve IDs to names
    try:
        names_resp = await client.post(
            "https://esi.evetech.net/latest/universe/names/",
            json=top_fleetmate_ids
        )
        names_data = names_resp.json()
    except Exception:
        names_data = []

    id_to_name = {entry["id"]: entry["name"] for entry in names_data if "id" in entry and "name" in entry}

    result = []
    for char_id, count in fleetmate_counter.most_common(name_limit):
        result.append({
            "name": id_to_name.get(char_id, f"Unknown ({char_id})"),
            "kills": count
        })

    return result

@app.get("/api/character/{name}")
async def get_character(name: str):
    cache_key = f"character:{name.lower()}"

    cached = await redis_client.get(cache_key)
    if cached:
        result = json.loads(cached)
        result["from_cache"] = True
        return result

    headers = {"User-Agent": "eve-intel-app (contact: your-email@example.com)"}
    async with httpx.AsyncClient(headers=headers, follow_redirects=True, timeout=30.0) as client:
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

            for ship in extract_top_list(top_lists, "shipType"):
                top_ships.append({
                    "name": ship.get("shipName"),
                    "kills": ship.get("kills"),
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

        # Real fleetmates, found by scanning actual killmails
        top_characters = await get_fleetmates(client, character_id, kill_limit=25, name_limit=15)

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
