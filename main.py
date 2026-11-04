import os
import json

from google import genai
from google.genai import types

from dotenv import load_dotenv
from openrouter import OpenRouter

load_dotenv()

api_key = os.getenv("OPENROUTER_API_KEY")
client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))

weeks = []

def recognize_image(file_bytes: bytes) -> str:
    response = client.models.generate_content(
        model="gemini-3.1-flash-lite",
        contents=[
            types.Part.from_text(
                text='Extract the schedule from the image and return only valid JSON. Each day is an array of objects with "number", "subject", and "homework". Keep the lesson order and include repeated subjects. If homework is not visible, use an empty string. Ignore all irrelevant information. Numbers should be arabic. Keep the day names in English. Do not use ```json formatting.'
            ),
            types.Part.from_bytes(
                data=file_bytes,
                mime_type="image/png",
            ),
        ],
    )

    return response.text

def send_prompt(prompt: str, json_list: list) -> str:
    with OpenRouter(api_key=api_key) as open_router:
        with open("./assets/SYSTEM_PROMPT.txt", "r", encoding="utf-8") as f:
            system_prompt = f.read()
        with open('./assets/output.json', 'r', encoding='utf-8') as file:
            output_json = file.read()
        res = open_router.chat.send(
            model="nvidia/nemotron-3.5-lightning:free",
            messages=[
                {
                    "role": "system",
                    "content": system_prompt+"ПОСЛЕ ЭТОГО СООБЩЕНИЯ ИДЁТ OUTPUT.JSON:"+output_json
                },
                {
                    "role": "user",
                    "content": "Это тестовый промпт, кратко выпиши что было в output.json"
                }
            ],
        )
        print(res, "\n-----------\n", res.choices[0].message.content)

# with open("./assets/image2.png", "rb") as f:
#     incoming_file_bytes = f.read()

# data = recognize_image(incoming_file_bytes)
# with open("homework.json", "w") as f:
#         json.dump(a, f, indent=4)
def getHomework():
    with open("homework.json", "r") as f:
            return json.load(f)

def setHomework(date:int, pair:int, homework:string):
     dnp = f"{date}:{pair}"
     print(dnp)
     dict = getHomework()
     dict[dnp] = homework
     print(type(dnp))
     with open ("homework.json", "w") as f:
          return json.dump(dict, f, indent=4)


setHomework(0, 1, "test3")     
sliced = {k: v for i, (k, v) in enumerate(getHomework().items()) if 0 <= i < 5}

print(sliced) 
# for i in range(6):
#     for j in range (6):
#         print(f'"{i}:{j}":"n{j}", ', end='')
# with open("output.json", "w", encoding="utf-8") as file:
#     json.dump(json.loads(data), file, indent=4, ensure_ascii=False)
