import json
import os

DATA_FILE = 'data/crossword.json'

def save_data(data):
    os.makedirs(os.path.dirname(DATA_FILE), exist_ok=True)  # Ապահովում ենք, որ `data/` թղթապանակը գոյություն ունի
    with open(DATA_FILE, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print("Պահպանվեց JSON")


def load_data():
    if not os.path.exists(DATA_FILE):
        return {}
    with open(DATA_FILE, 'r', encoding='utf-8') as f:
        return json.load(f)


