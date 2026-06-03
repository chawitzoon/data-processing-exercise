import os
import re
import json
import pandas as pd
import numpy as np
from typing import List, Optional
from pydantic import BaseModel, Field
from openai import OpenAI

client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY"))
BASE_DIR = "take-home-case"


# --- Verification Output Schema ---
class MatchDecisionPayload(BaseModel):
    matched_standardizedItemId: Optional[str] = Field(description="The unique ID from KVI master if a true match is confirmed; null otherwise.")
    confidence: float = Field(description="Match certainty score ranging strict boundaries from 0.0 to 1.0.")
    decision: str = Field(description="Must evaluate to exactly one of: 'match', 'no_match', or 'uncertain'.")
    reason: str = Field(description="Exhaustive step-by-step semantic comparison verifying size, units, and brand presence.")

# --- Local Vector DB for Pre-Filtering ---
class SimpleLocalVectorDB:
    """
    A lightweight memory-cached Vector Database executing fast matrix operations via numpy 
    for calculating local cosine similarities without heavy external dependencies.
    """
    def __init__(self, embedding_model: str = "text-embedding-3-small"):
        self.model = embedding_model
        self.vectors = []
        self.metadata = []

    def get_embedding(self, text: str) -> List[float]:
        # Safely convert incoming inputs to clean lowercase text snippets
        clean_text = str(text).strip().lower()
        if not clean_text:
            return [0.0] * 1536
        response = client.embeddings.create(input=[clean_text], model=self.model)
        return response.data[0].embedding

    def fit(self, df: pd.DataFrame, text_column: str, id_column: str):
        print(f"Generating vector index cache for {len(df)} canonical items using {self.model}...")
        texts = df[text_column].astype(str).tolist()
        
        # Batch requests to maximize OpenAI throughput
        batch_size = 100
        for i in range(0, len(texts), batch_size):
            batch = texts[i:i+batch_size]
            response = client.embeddings.create(input=batch, model=self.model)
            self.vectors.extend([item.embedding for item in response.data])
        
        self.vectors = np.array(self.vectors)
        self.metadata = df.to_dict(orient="records")

    def query(self, text: str, top_k: int = 3) -> List[dict]:
        if len(self.vectors) == 0:
            return []
        query_vector = np.array(self.get_embedding(text))
        
        # Matrix multiplication to find exact local cosine similarities
        norms = np.linalg.norm(self.vectors, axis=1) * np.linalg.norm(query_vector)
        norms[norms == 0] = 1e-9  # Prevent divide-by-zero errors
        similarities = np.dot(self.vectors, query_vector) / norms
        
        top_indices = np.argsort(similarities)[::-1][:top_k]
        results = []
        for idx in top_indices:
            item = self.metadata[idx].copy()
            item["_similarity_score"] = float(similarities[idx])
            results.append(item)
        return results
    

CORRUPTION_PATTERNS = [
    # Known catastrophic corruption cases
    (r"(?i)\bลิตรED\b", "LED"),
    (r"(?i)\bBUND\s*ลิตรE\b", "BUNDLE"),
    (r"(?i)\bลิตรemon\b", "Lemon"),
    (r"(?i)\bลิตรime\b", "Lime"),
    (r"(?i)\bลิตรife\b", "Life"),
    (r"(?i)\bลิตรig\b", "Lig"),
    (r"(\d+(?:\.\d+)?)\s*ลิตร\b", r"\1 L"), # "1.25ลิตร" -> "1.25 L"
]

def repair_corrupted_text(text: str) -> str:
    s = text

    for pattern, replacement in CORRUPTION_PATTERNS:
        s = re.sub(pattern, replacement, s)

    return s

# --- Deterministic Pre-Processing ---
def deterministic_clean_title(title: str) -> str:
    """
    Executes raw string level cleaning and repairs structural file bugs 
    prior to vector operations or model interaction to optimize matching.
    """
    if pd.isna(title):
        return ""
    
    s = str(title)
    # Phase 2 Goal: Reverse the global search-and-replace 'ลิตร' corruption back to 'L'
    # Centralized corruption repair 
    s = repair_corrupted_text(s)
    
    # Strip aggressive promotional banners and regional marketplace noise text
    promo_patterns = [
        r"(?i)flash sale\s*(-\d+%)?",
        r"(?i)buy\s*1\s*get\s*1\s*(free)?",
        r"(?i)super\s*save",
        r"(?i)promotion",
        r"(?i)promo\s*(-\d+%)?",
        r"(?i)pack\s*\d+",
        r"(?i)set\s*\d+\s*ขวด",
        r"(?i)limited\s*!",
        r"(?i)new\s*!"
    ]
    for pattern in promo_patterns:
        s = re.sub(pattern, "", s)
        
    # Clean up excess white spacing artifacts
    s = re.sub(re.compile(r'\s+'), ' ', s).strip()
    return s
def run_ingestion():
    print("=== Launching Phase 2: Ingestion & Catalog Matching ===")
    
    if not os.path.exists(os.path.join(BASE_DIR, "source_raw.csv")) or not os.path.exists(os.path.join(BASE_DIR, "kvi_master.csv")):
        raise FileNotFoundError("Missing source assets.")

    # Load source materials into active dataframes
    source_df = pd.read_csv(os.path.join(BASE_DIR, "source_raw.csv"))
    kvi_df = pd.read_csv(os.path.join(BASE_DIR, "kvi_master.csv"))

    vdb = SimpleLocalVectorDB()
    vdb.fit(kvi_df, text_column="productName", id_column="standardizedItemId")

    # eval_subset = source_df.head(200).copy()
    eval_subset = source_df.copy()
    matched_results_collector = []

    print(f"\nProcessing {len(eval_subset)} row candidate subset blocks...")
    
    for idx, row in eval_subset.iterrows():
        source_id = row["id"]
        raw_title = row["product_title"]
        
        # Apply local deterministic pre-cleaning and reverse the 'ลิตร' corruption bug
        clean_title = deterministic_clean_title(raw_title)
        
        candidates = vdb.query(clean_title, top_k=3)
        
        # Hard routing rule: If vector similarity is too low, bypass LLM entirely
        if not candidates or candidates[0]["_similarity_score"] < 0.25:
            matched_results_collector.append({
                "source_row_id": source_id,
                "matched_standardizedItemId": None,
                "confidence": 0.0,
                "decision": "no_match",
                "reason": "Filtered by local retrieval layer: Similarity score fell well below minimum structural threshold."
            })
            continue

        # Execute deep cross-encoder logical evaluations against the retrieved product options
        verification_prompt = f"""
        Determine if the competitor product entry matches any item from our canonical master list.
        Verify that brand identifiers, model names, size quantities, and physical metric units match exactly.
        If the competitor item describes a different flavor, size, variant, or an unlisted product, return 'no_match'.
        
        Competitor Source Product Row:
        - Raw Title: {raw_title}
        - Cleaned Title: {clean_title}
        - Price Context: {row.get('list_price')} (Discounted: {row.get('discount_price')})
        
        Top Available Master KVI Candidates:
        {json.dumps(candidates, indent=2)}
        
        Choose the correct ID if a match is found. Output strict structured JSON format data.
        """

        try:
            response = client.beta.chat.completions.parse(
                model="gpt-4o-mini",
                messages=[{"role": "user", "content": verification_prompt}],
                response_format=MatchDecisionPayload
            )
            
            decision = response.choices[0].message.parsed
            # Map structural properties straight into our collection ledger array
            matched_results_collector.append({
                "source_row_id": source_id,
                "matched_standardizedItemId": decision.matched_standardizedItemId,
                "confidence": round(decision.confidence, 4),
                "decision": decision.decision.lower(),
                "reason": decision.reason
            })
            
        except Exception as e:
            # Fallback error recovery boundary logic to guarantee pipeline processing continuity
            matched_results_collector.append({
                "source_row_id": source_id,
                "matched_standardizedItemId": None,
                "confidence": 0.0,
                "decision": "uncertain",
                "reason": f"Execution exception: {str(e)}"
            })

    output_df = pd.DataFrame(matched_results_collector)
    output_df.to_csv("matched_rows.csv", index=False)
    print("\nSuccess: Generated target evaluation file 'matched_rows.csv'.")

if __name__ == "__main__":
    run_ingestion()