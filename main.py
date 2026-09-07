from fastapi import FastAPI
import httpx

app = FastAPI()

@app.get("/")
def read_root():
    return {"status": "EVE Intel backend is running"}

@app.get("/character/{name}")
async def get_character(name: str):
    headers = {"User-Agent": "eve-intel-app (contact: your-email@example.com)"}
    async with httpx.AsyncClient(headers=headers) as client:
        # Step 1: Convert the character name into an ID
        search_resp = await client.post(
            "https://esi.evetech.net/latest/universe/ids/",
            json=[name]
        )
        search_data = search_resp.json()

        if "characters" not in search_data or len(search_data["characters"]) == 0:
            return {"error": f"No character found with name '{name}'"}

        character_id = search_data["characters"][0]["id"]

        # Step 2: Get full character details using the ID
        detail_resp = await client.get(
            f"https://esi.evetech.net/latest/characters/{character_id}/"
        )
        details = detail_resp.json()

        corporation_id = details.get("corporation_id")
        alliance_id = details.get("alliance_id")

        # Step 3: Get the corporation name
        corp_name = None
        if corporation_id:
            corp_resp = await client.get(
                f"https://esi.evetech.net/latest/corporations/{corporation_id}/"
            )
            corp_name = corp_resp.json().get("name")

        # Step 4: Get the alliance name (if they're in one)
        alliance_name = None
        if alliance_id:
            alliance_resp = await client.get(
                f"https://esi.evetech.net/latest/alliances/{alliance_id}/"
            )
            alliance_name = alliance_resp.json().get("name")

        # Step 5: Get zKillboard stats (debug version)
        zkill_stats = {}
        try:
            zkill_resp = await client.get(
                f"https://zkillboard.com/api/stats/characterID/{character_id}/"
            )
            zkill_stats = {
                "debug_status_code": zkill_resp.status_code,
                "debug_raw_text": zkill_resp.text[:500],
            }
        except Exception as e:
            zkill_stats = {"error": f"Exception occurred: {str(e)}"}

        return {
            "name": name,
            "character_id": character_id,
            "security_status": details.get("security_status"),
            "birthday": details.get("birthday"),
            "corporation_id": corporation_id,
            "corporation_name": corp_name,
            "alliance_id": alliance_id,
            "alliance_name": alliance_name,
            "zkillboard": zkill_stats,
        }
