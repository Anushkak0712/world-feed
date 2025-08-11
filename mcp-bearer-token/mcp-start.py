# This server shows how you can use the unique identifier for users sent by puch in every tool call.
# This server is a news recommendation MCP server where users can set interests and get personalized news.

import asyncio
from typing import Annotated, Optional, Literal
import os, uuid, json
from datetime import datetime
from dotenv import load_dotenv
import requests

from fastmcp import FastMCP
from fastmcp.server.auth.providers.bearer import BearerAuthProvider, RSAKeyPair
from mcp.server.auth.provider import AccessToken
from mcp import ErrorData, McpError
from mcp.types import TextContent, INVALID_PARAMS, INTERNAL_ERROR
from pydantic import Field, BaseModel

# --- Env ---
load_dotenv()
TOKEN = os.environ.get("AUTH_TOKEN")
MY_NUMBER = os.environ.get("MY_NUMBER")
NEWS_API_KEY = os.environ.get("NEWS_API_KEY")  # You'll need a news API key
assert TOKEN, "Please set AUTH_TOKEN in your .env file"
assert MY_NUMBER is not None, "Please set MY_NUMBER in your .env file"
assert NEWS_API_KEY, "Please set NEWS_API_KEY in your .env file"

# --- Auth ---
class SimpleBearerAuthProvider(BearerAuthProvider):
    def __init__(self, token: str):
        k = RSAKeyPair.generate()
        super().__init__(
            public_key=k.public_key, jwks_uri=None, issuer=None, audience=None
        )
        self.token = token

    async def load_access_token(self, token: str) -> AccessToken | None:
        if token == self.token:
            return AccessToken(
                token=token, client_id="news-client", scopes=["*"], expires_at=None
            )
        return None

mcp = FastMCP(
    "News Recommendation MCP Server",
    auth=SimpleBearerAuthProvider(TOKEN),
)

# JSON-based persistent storage for user interests
USER_INTERESTS_FILE = "user_interests.json"

def load_user_interests() -> dict[str, list[str]]:
    if os.path.exists(USER_INTERESTS_FILE):
        try:
            with open(USER_INTERESTS_FILE, 'r') as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            return {}
    return {}

def save_user_interests(interests: dict[str, list[str]]):
    try:
        with open(USER_INTERESTS_FILE, 'w') as f:
            json.dump(interests, f, indent=2)
    except IOError as e:
        print(f"Error saving user interests: {e}")

# Initialize user interests from JSON
USER_INTERESTS = load_user_interests()

def _user_interests(puch_user_id: str) -> list[str]:
    if not puch_user_id:
        raise McpError(
            ErrorData(code=INVALID_PARAMS, message="puch_user_id is required")
        )
    return USER_INTERESTS.setdefault(puch_user_id, [])

def _error(code, msg):
    raise McpError(ErrorData(code=code, message=msg))

# --- Rich Tool Description models ---
class RichToolDescription(BaseModel):
    description: str
    use_when: str
    side_effects: str | None = None

# --- Tool: validate (required by Puch) ---
@mcp.tool
async def validate() -> str:
    return MY_NUMBER

# --- Tool descriptions (rich) ---
HELLO_BUZZBOT_DESCRIPTION = RichToolDescription(
    description="Start a conversation with BuzzBot to set up news interests or add more and get personalized news.",
    use_when="The user says hello, hi, or wants to start using the news service.",
    side_effects="Initiates a guided conversation to set up user interests.",
)

SET_INTERESTS_DESCRIPTION = RichToolDescription(
    description="Set or update a user's news interests (topics they want to read about).",
    use_when="The user wants to specify what types of news they're interested in (e.g., technology, sports, politics).",
    side_effects="Replaces the user's existing interests with the new list and saves to persistent storage.",
)

GET_INTERESTS_DESCRIPTION = RichToolDescription(
    description="Get a user's current news interests.",
    use_when="The user wants to see what topics they've set as interests.",
    side_effects="None.",
)

GET_LATEST_NEWS_DESCRIPTION = RichToolDescription(
    description="Get the latest 5 news articles based on a user's interests.",
    use_when="The user wants to see recent news articles related to their interests.",
    side_effects="Fetches fresh news from external API based on user's stored interests.",
)

# --- Tools ---
@mcp.tool(description=HELLO_BUZZBOT_DESCRIPTION.model_dump_json())
async def hello_buzzbot(
    puch_user_id: Annotated[str, Field(description="Puch User Unique Identifier")],
    user_message: Annotated[str, Field(description="User's greeting message (e.g., 'hello', 'hi', 'hello buzzbot')")],
) -> list[TextContent]:
    try:
        user_message_lower = user_message.lower().strip()
        
        # Check if user has existing interests
        existing_interests = _user_interests(puch_user_id)
        
        if "hello" in user_message_lower or "hi" in user_message_lower:
            if existing_interests:
                # User has interests set, ask if they want news
                response = {
                    "user_id": puch_user_id,
                    "message": f"�� Hello! I'm BuzzBot, your personal news assistant! I see you're interested in: {', '.join(existing_interests)}. Would you like me to get you the latest news on these topics? Just say 'yes' or 'get news'!",
                    "current_interests": existing_interests,
                    "next_action": "ready_for_news",
                    "suggestions": ["Say 'yes' or 'get news' to see latest articles", "Say 'change interests' to update your topics"]
                }
            else:
                # New user, guide them to set interests
                response = {
                    "user_id": puch_user_id,
                    "message": "�� Hello! I'm BuzzBot, your personal news assistant! I'd love to help you stay updated with news that matters to you. What topics are you interested in? You can tell me things like 'technology', 'sports', 'politics', 'science', etc. Just list your interests and I'll remember them!",
                    "current_interests": [],
                    "next_action": "need_interests",
                    "suggestions": ["List your interests (e.g., 'technology sports politics')", "Or say 'I like technology and science'"]
                }
            
            return [TextContent(type="text", text=json.dumps(response))]
        else:
            # Handle other messages
            response = {
                "user_id": puch_user_id,
                "message": "I'm here to help! Say 'hello' to get started, or if you already have interests set, just say 'get news' to see the latest articles!",
                "current_interests": existing_interests,
                "next_action": "general_help"
            }
            return [TextContent(type="text", text=json.dumps(response))]
            
    except Exception as e:
        _error(INTERNAL_ERROR, str(e))

@mcp.tool(description=SET_INTERESTS_DESCRIPTION.model_dump_json())
async def set_interests(
    puch_user_id: Annotated[str, Field(description="Puch User Unique Identifier")],
    interests: Annotated[list[str], Field(description="List of news topics of interest (e.g., ['technology', 'sports', 'politics'])")],
) -> list[TextContent]:
    try:
        if not interests or not isinstance(interests, list):
            _error(INVALID_PARAMS, "interests must be a non-empty list")
        
        # Clean and validate interests
        clean_interests = [interest.strip().lower() for interest in interests if interest.strip()]
        if not clean_interests:
            _error(INVALID_PARAMS, "interests cannot be empty after cleaning")
        
        # Update user interests
        USER_INTERESTS[puch_user_id] = clean_interests
        
        # Save to JSON file
        save_user_interests(USER_INTERESTS)
        
        result = {
            "user_id": puch_user_id,
            "interests": clean_interests,
            "message": f"🎉 Perfect! I've saved your interests: {', '.join(clean_interests)}. Now you're all set to get personalized news! Would you like me to fetch the latest articles for you? Just say 'yes' or 'get news'!",
            "next_action": "ready_for_news",
            "suggestions": ["Say 'yes' or 'get news' to see latest articles", "Say 'show interests' to see what I saved"]
        }
        return [TextContent(type="text", text=json.dumps(result))]
    except McpError:
        raise
    except Exception as e:
        _error(INTERNAL_ERROR, str(e))

@mcp.tool(description=GET_INTERESTS_DESCRIPTION.model_dump_json())
async def get_interests(
    puch_user_id: Annotated[str, Field(description="Puch User Unique Identifier")],
) -> list[TextContent]:
    try:
        interests = _user_interests(puch_user_id)
        result = {
            "user_id": puch_user_id,
            "interests": interests,
            "count": len(interests),
            "message": f"Here are your current interests: {', '.join(interests) if interests else 'None set yet'}",
            "next_action": "show_interests"
        }
        return [TextContent(type="text", text=json.dumps(result))]
    except Exception as e:
        _error(INTERNAL_ERROR, str(e))

@mcp.tool(description=GET_LATEST_NEWS_DESCRIPTION.model_dump_json())
async def get_latest_news(
    puch_user_id: Annotated[str, Field(description="Puch User Unique Identifier")],
) -> list[TextContent]:
    try:
        interests = _user_interests(puch_user_id)
        
        if not interests:
            _error(INVALID_PARAMS, "No interests set. Please set interests first using set_interests or say 'hello' to get started.")
        
        # Fetch news based on user interests
        news_articles = await fetch_news_by_interests(interests)
        
        if not news_articles:
            result = {
                "user_id": puch_user_id,
                "interests": interests,
                "message": "📰 I tried to fetch news for your interests, but couldn't find any recent articles right now. This sometimes happens with very specific topics. Would you like to try different interests or check back later?",
                "news_count": 0,
                "articles": [],
                "next_action": "no_news_found"
            }
        else:
            result = {
                "user_id": puch_user_id,
                "interests": interests,
                "news_count": len(news_articles),
                "message": f"📰 Here are the latest {len(news_articles)} news articles based on your interests ({', '.join(interests)}):",
                "articles": news_articles,
                "next_action": "news_displayed",
                "suggestions": ["Say 'more news' to refresh", "Say 'change interests' to update topics"]
            }
        
        return [TextContent(type="text", text=json.dumps(result))]
    except McpError:
        raise
    except Exception as e:
        _error(INTERNAL_ERROR, str(e))

# ... existing code ...

async def fetch_news_by_interests(interests: list[str]) -> list[dict]:
    """Fetch latest news articles based on user interests using NewsAPI"""
    try:
        # Use the first interest as the main query, combine others for broader coverage
        main_interest = interests[0]
        query = " OR ".join(interests[:3])  # Use up to 3 interests
        
        url = "https://newsapi.org/v2/everything"
        params = {
            "q": query,
            "sortBy": "publishedAt",
            "pageSize": 5,  # Get exactly 5 articles
            "language": "en",
            "apiKey": NEWS_API_KEY
        }
        
        response = requests.get(url, params=params, timeout=10)
        response.raise_for_status()
     
        
        data = response.json()
        articles = data.get("articles", [])
        
        # Format articles for consistent output
        formatted_articles = []
        for article in articles[:5]:  # Ensure we only return 5
            formatted_articles.append({
                "title": article.get("title", ""),
                "description": article.get("description", ""),
                "url": article.get("url", ""),
                "published_at": article.get("publishedAt", ""),
                "source": article.get("source", {}).get("name", ""),
                "relevance_score": calculate_relevance(article, interests)
            })
        
        # Sort by relevance score (highest first)
        formatted_articles.sort(key=lambda x: x["relevance_score"], reverse=True)
        
        return formatted_articles[:5]
        
    except requests.RequestException as e:
        print(f"Error fetching news: {e}")
        return []
    except Exception as e:
        print(f"Unexpected error: {e}")
        return []


def calculate_relevance(article: dict, interests: list[str]) -> float:
    """Calculate how relevant an article is to user interests"""
    score = 0.0
    title = article.get("title", "").lower()
    description = article.get("description", "").lower()
    content = f"{title} {description}"
    
    for interest in interests:
        if interest.lower() in content:
            score += 1.0
    
    return score

# --- Run MCP Server ---
async def main():
    print("📰 Starting BuzzBot News Recommendation MCP server on http://0.0.0.0:8086")
    print("💾 User interests will be stored in user_interests.json")
    await mcp.run_async("streamable-http", host="0.0.0.0", port=8086)

if __name__ == "__main__":
    asyncio.run(main())