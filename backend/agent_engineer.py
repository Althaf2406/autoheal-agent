from __future__ import annotations

import ast
import os
import re

from dotenv import load_dotenv
from openai import OpenAI

# Load environment variables from .env file (same setup as the Agent Researcher)
load_dotenv(override=True)

NEBIUS_BASE_URL = "https://api.tokenfactory.nebius.com/v1/"

# Use NVIDIA Nemotron 3 Ultra model
MODEL_ID = "nvidia/Nemotron-3-Ultra-550b-a55b"

_client: OpenAI | None = None


def get_client() -> OpenAI:
    """Create the Nebius client on first use, so the backend can start even before the key is set."""
    global _client
    if _client is None:
        api_key = os.environ.get("NEBIUS_API_KEY")
        if not api_key:
            raise RuntimeError("NEBIUS_API_KEY is not set in .env")
        _client = OpenAI(base_url=NEBIUS_BASE_URL, api_key=api_key)
    return _client

SYSTEM_PROMPT = """You are Agent Engineer, a senior data engineer who repairs broken data pipelines.
You receive an error log, a Root Cause Analysis written by another agent, and the source file that failed.
Your job is to return a corrected version of that file.

Strict rules:
1. Return the COMPLETE corrected file, not a diff and not a snippet.
2. Change only what is needed to fix the error. Keep the rest of the file, its structure and its comments as they are.
3. Use only plain {language}. Do not add new third-party libraries unless the Root Cause Analysis says a missing library is the cause.
4. Do not invent data, files or columns that are not supported by the error log, the analysis or the source code.

Answer in exactly this format and nothing else:

SUMMARY:
<two or three sentences in plain English describing what you changed and why>

CODE:
```{fence}
<the complete corrected file>
```"""

LANGUAGE_BY_EXTENSION = {
    ".py": ("Python", "python"),
    ".sql": ("SQL", "sql"),
}


def detect_language(file_path: str | None) -> tuple[str, str]:
    """Return (language name, code fence tag) based on the file extension. Defaults to Python."""
    if file_path:
        ext = os.path.splitext(file_path)[1].lower()
        if ext in LANGUAGE_BY_EXTENSION:
            return LANGUAGE_BY_EXTENSION[ext]
    return LANGUAGE_BY_EXTENSION[".py"]


def parse_engineer_response(text: str) -> tuple[str, str | None]:
    """Split the LLM answer into (summary, code). Code is None if no code block was found."""
    # Nemotron is a reasoning model and may put its thinking in <think> tags before the answer
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"^.*</think>", "", text, flags=re.DOTALL | re.IGNORECASE)

    summary_match = re.search(r"SUMMARY:\s*(.*?)(?:\n\s*CODE:|```)", text, re.DOTALL | re.IGNORECASE)
    summary = summary_match.group(1).strip() if summary_match else ""

    code_blocks = re.findall(r"```[a-zA-Z0-9_+-]*\s*\n(.*?)```", text, re.DOTALL)
    code = code_blocks[-1] if code_blocks else None
    if code is not None and not code.endswith("\n"):
        code += "\n"
    return summary, code


def validate_code(code: str, language: str) -> str | None:
    """Return an error message if the code is not valid, otherwise None. Only Python is checked."""
    if language != "Python":
        return None
    try:
        ast.parse(code)
    except SyntaxError as e:
        return f"SyntaxError on line {e.lineno}: {e.msg}"
    return None


def build_user_prompt(error_message: str, root_cause_analysis: str, file_path: str | None, original_code: str | None) -> str:
    if original_code:
        source_section = f"FILE PATH: {file_path}\n\nCURRENT SOURCE CODE:\n```\n{original_code}\n```"
    else:
        source_section = (
            "The source file could not be retrieved. Write a minimal, complete script that performs the "
            "task implied by the error and avoids the failure."
        )
    return f"""ERROR LOG:
{error_message}

ROOT CAUSE ANALYSIS (from Agent Researcher):
{root_cause_analysis}

{source_section}"""


def run_engineer(error_message: str, root_cause_analysis: str, file_path: str | None = None,
                 original_code: str | None = None, max_attempts: int = 2) -> dict:
    """
    Generate a fixed version of the broken file.

    Returns a dict with:
      - summary: short explanation of the change
      - fixed_code: full corrected file content (or None on failure)
      - language: "Python" or "SQL"
      - valid: True if the code passed the syntax check
      - error: message if something went wrong
    """
    print("\n🛠️ [Agent Engineer] Starting fix generation...")
    language, fence = detect_language(file_path)

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT.format(language=language, fence=fence)},
        {"role": "user", "content": build_user_prompt(error_message, root_cause_analysis, file_path, original_code)},
    ]

    summary, code, problem = "", None, None
    for attempt in range(1, max_attempts + 1):
        print(f"🧠 [Agent Engineer] Asking Nemotron for a fix (attempt {attempt}/{max_attempts})...")
        response = get_client().chat.completions.create(
            model=MODEL_ID,
            messages=messages,
            max_tokens=8000,
            temperature=0.1,
        )
        choice = response.choices[0]
        answer = choice.message.content or ""
        summary, code = parse_engineer_response(answer)

        if choice.finish_reason == "length":
            problem = "The answer was cut off before the end. Keep the summary short and return only the file."
        elif code is None:
            problem = "The answer did not contain a code block."
        elif original_code is not None and code.strip() == original_code.strip():
            problem = "The returned code is identical to the original file."
        else:
            problem = validate_code(code, language)

        if problem is None:
            print("✅ [Agent Engineer] Fix generated and passed validation.\n")
            return {"summary": summary, "fixed_code": code, "language": language, "valid": True, "error": None}

        print(f"⚠️ [Agent Engineer] Fix rejected: {problem}")
        # Give the model its own answer plus the problem and ask again
        messages.append({"role": "assistant", "content": answer})
        messages.append({"role": "user", "content": f"Your answer was rejected: {problem} "
                                                     "Fix this and answer again in the exact required format."})

    print("❌ [Agent Engineer] Could not produce a valid fix.\n")
    return {"summary": summary, "fixed_code": code, "language": language, "valid": False, "error": problem}


# Internal testing block (only runs if this file is executed directly)
if __name__ == "__main__":
    dummy_code = 'import pandas as pd\n\ndf = pd.read_csv("data/sales.csv")\ndf["revenue"] = df["total_price"] * 1.1\n'
    dummy_error = "KeyError: 'total_price'"
    dummy_rca = "The CSV has columns 'price' and 'quantity'. There is no 'total_price' column; compute it as price * quantity."
    result = run_engineer(dummy_error, dummy_rca, "etl/pipeline.py", dummy_code)
    print(result["summary"])
    print(result["fixed_code"])
