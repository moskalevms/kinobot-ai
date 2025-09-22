import os
import requests

token = os.getenv("GIGACHAT_API_KEY")
if not token:
    raise Exception("GIGACHAT_API_KEY не установлен")

url = "https://gigachat.devices.sberbank.ru/api/v1/chat/completions"

headers = {
    "Authorization": f"Bearer {token}",
    "Content-Type": "application/json"
}

data = {
    "model": "GigaChat",
    "messages": [
        {"role": "user", "content": "Привет, ты кто?"}
    ],
    "temperature": 0.3,
    "max_tokens": 50
}

response = requests.post(url, headers=headers, json=data, verify=False)
print("Status Code:", response.status_code)
print("Response:", response.json())