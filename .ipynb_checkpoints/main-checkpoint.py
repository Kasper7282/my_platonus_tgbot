import requests

def recognize_image(file):
    with open("./image.png", "rb") as f:
	    image_bytes = f.read()
    
    response = requests.post(
        "https://api.ocr.space/parse/image",
        files={"file": ("image.png", image_bytes, "image/png")},
        data={
            "apikey": "K88320491088957",  # Поставьте свой ключ
            "language": "rus",
            "OCREngine": "2",
        },
    )

    result = response.json()

    return result["ParsedResults"][0]["ParsedText"]
