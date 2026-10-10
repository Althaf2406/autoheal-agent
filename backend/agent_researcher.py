import os
from dotenv import load_dotenv
from openai import OpenAI
from tavily import TavilyClient

# Load environment variables from .env file (override is important so it picks up key updates)
load_dotenv(override=True)

# Setup connection to Nebius and Tavily
nebius_client = OpenAI(
    base_url="https://api.tokenfactory.nebius.com/v1/",
    api_key=os.environ.get("NEBIUS_API_KEY")
)

# Use NVIDIA Nemotron 3 Ultra model
MODEL_ID = "nvidia/Nemotron-3-Ultra-550b-a55b"

def run_researcher(error_message: str) -> str:
    print(f"\n🕵️ [Agent Researcher] Starting error analysis...")
    
    # --- Step 1: Extract Keyword for Web Search ---
    response = nebius_client.chat.completions.create(
        model=MODEL_ID,
        messages=[
            {"role": "system", "content": "You are a Google Search query generator. Your ONLY job is to output a single, short search query string to find a solution for the provided error. DO NOT include any conversational text, explanations, or quotes. Output ONLY the raw query string."},
            {"role": "user", "content": f"Error: {error_message}"}
        ],
        max_tokens=20,
        temperature=0.1
    )
    search_query = response.choices[0].message.content.strip().replace('"', '')
    print(f"🔍 [Agent Researcher] Searching the internet with query: '{search_query}'")
    
    # --- Step 2: Research Solutions using Tavily ---
    tavily_api_key = os.environ.get("TAVILY_API_KEY")
    tavily_context = ""
    
    if not tavily_api_key or "masukkan" in tavily_api_key.lower():
        tavily_context = "[Web Search Skipped: Tavily API Key is not configured.]"
        print("⚠️ [Agent Researcher] Skipping Tavily because API Key is missing.")
    else:
        try:
            tavily_client = TavilyClient(api_key=tavily_api_key)
            tavily_response = tavily_client.search(query=search_query, search_depth="basic", max_results=3)
            # Combine title and content of the search results
            tavily_context = "\n".join([f"- {res['title']}: {res['content']}" for res in tavily_response['results']])
            print(f"✅ [Agent Researcher] Found {len(tavily_response['results'])} reference articles from the web.")
        except Exception as e:
            tavily_context = f"[Failed to fetch Tavily data: {e}]"
            print(tavily_context)
            
    # --- Step 3: Compile Root Cause Analysis ---
    analysis_prompt = f"""
    You are a Senior AI Data Engineer (Agent Researcher).
    Your task is to create a professional and concise "Root Cause Analysis" regarding the data pipeline error that occurred.
    
    ORIGINAL ERROR:
    {error_message}
    
    CONTEXT / REFERENCES FROM THE INTERNET (Tavily):
    {tavily_context}
    
    Write an analysis focusing on:
    1. What is the root cause of the error?
    2. What technical steps should be taken to fix it in the code?
    
    Write the response in clean English (Markdown format).
    """
    
    print("🧠 [Agent Researcher] Summarizing research and writing Root Cause Analysis...")
    analysis_response = nebius_client.chat.completions.create(
        model=MODEL_ID,
        messages=[{"role": "user", "content": analysis_prompt}],
        max_tokens=800
    )
    
    root_cause_analysis = analysis_response.choices[0].message.content
    print("✅ [Agent Researcher] Analysis Completed!\n")
    
    return root_cause_analysis

# Internal testing block (only runs if this file is executed directly)
if __name__ == "__main__":
    dummy_error = "ModuleNotFoundError: No module named 'pandas'"
    print(run_researcher(dummy_error))
