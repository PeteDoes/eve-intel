from fastapi import FastAPI
import httpx

app = FastAPI()

@app.get("/")
def read_root():
    return {"status": "EVE Intel backend is running"}

@app.get("/character/{name}")
async def get_character(name: str):
    async with httpx.AsyncClient() as client:
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

        return {
            "name": name,
            "character_id": character_id,
            "details": details
        }
