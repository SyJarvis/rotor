  curl -X POST http://localhost:8000/api/admin/channels \
    -H "Content-Type: application/json" \
    -d '{
      "name": "Moonshot",
      "type": "moonshot",
      "key": "你的API密钥",
      "base_url": "https://api.moonshot.cn/v1",
      "models": ["moonshot-v1-8k"],
      "enabled": true
    }

curl -X POST http://localhost:8000/api/admin/channels \
-H "Content-Type: application/json" \
-d '{
"name": "Moonshot",
"type": "moonshot",
"key": "your-provider-api-key",
"base_url": "https://api.minimaxi.com/anthropic",
"models": ["MiniMax-M2.5"],
"enabled": true
zhelchangshang
