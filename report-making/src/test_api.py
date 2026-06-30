from openai import OpenAI
from dotenv import load_dotenv
load_dotenv()
import os

client = OpenAI(
  base_url="https://openrouter.ai/api/v1",
  api_key=os.getenv("OPENROUTER_API_KEY"),
)

completion = client.chat.completions.create(
  extra_headers={
    "HTTP-Referer": "<YOUR_SITE_URL>", # Optional. Site URL for rankings on openrouter.ai.
    "X-Title": "<YOUR_SITE_NAME>", # Optional. Site title for rankings on openrouter.ai.
  },
  extra_body={},
  model="google/gemini-2.5-flash-lite",
  messages=[
    {
      "role": "user",
      "content": [
        {
          "type": "text",
          "text": "Hello just testing the API, you recieved this message say YES, else NO"
        }
      ]
    }
  ]
)
print(completion.choices[0].message.content)