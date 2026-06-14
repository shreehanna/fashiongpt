
from flask import Flask, render_template_string, request, jsonify
import anthropic
import json
import os

client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))

CLOSET_FILE = "closet.json"

def load_closet():
    if os.path.exists(CLOSET_FILE):
        with open(CLOSET_FILE, "r") as f:
            return json.load(f)
    return []

def save_closet(items):
    with open(CLOSET_FILE, "w") as f:
        json.dump(items, f)

@app.route('/')
def index():
    return open('index.html').read()
@app.route('/api/outfit', methods=['POST'])
def outfit():
    data = request.json
    message = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        messages=[{"role": "user", "content": data['prompt']}]
    )
    return jsonify({"outfit": message.content[0].text})

@app.route('/api/closet/save', methods=['POST'])
def closet_save():
    data = request.json
    save_closet(data.get('items', []))
    return jsonify({"ok": True})

@app.route('/api/closet/load')
def closet_load():
    return jsonify({"items": load_closet()})

if __name__ == '__main__':
    app.run(debug=True, port=5000)