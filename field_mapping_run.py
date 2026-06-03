import os
import json
import pandas as pd
import numpy as np
from typing import List
from pydantic import BaseModel, Field
from openai import OpenAI

client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY"))
BASE_DIR = "take-home-case"
source_csv_path = os.path.join(BASE_DIR, "source_raw.csv")
schema_json_path = os.path.join(BASE_DIR, "standard_schema.json")

# --- Strict Pydantic Output Schema ---
class FieldMappingDetail(BaseModel):
    source_field: str = Field(description="The exact header name from the raw competitor source file.")
    standard_field: str = Field(description="The canonical field name from standard_schema.json it aligns to.")
    reasoning: str = Field(description="Detailed structural and semantic explanation for this field pairing choice.")
    transformation_required: str = Field(description="Regex pattern, cleaning rule, or extraction method needed to clean the field.")

class StructuralSchemaMapping(BaseModel):
    mappings: List[FieldMappingDetail]

def run_onboarding():
    print("=== Launching Phase 1: Structural Schema Alignment ===")
    
    if not os.path.exists(source_csv_path) or not os.path.exists(schema_json_path):
        raise FileNotFoundError("Missing source_raw.csv or standard_schema.json.")

    source_df = pd.read_csv(source_csv_path)
    with open(schema_json_path, "r", encoding="utf-8") as f:
        schema_data = json.load(f)

    # Extract 20 diverse rows to give the model a complete view of the data variance
    sample_size = min(20, len(source_df))
    sample_indices = np.linspace(0, len(source_df) - 1, sample_size, dtype=int)
    diverse_sample = source_df.iloc[sample_indices]
    
    print(f"Profiling {sample_size} records to construct canonical schema definitions...")
    
    alignment_prompt = f"""
    You are an expert Data Solutions Architect onboarding a new competitor price stream.
    Analyze the canonical target JSON schema design and the raw sample data matrix provided below.
    Map each raw source column field to its target destination canonical schema attribute.
    
    Target Schema Blueprint:
    {json.dumps(schema_data, indent=2)}
    
    Raw Source Field Sample Rows (Stratified Set):
    {diverse_sample.to_json(orient='records', indent=2)}
    
    Identify any necessary cleansing or extraction steps (e.g., stripping local currency markers, text unit normalization).
    """

    response = client.beta.chat.completions.parse(
        model="gpt-4o",
        messages=[{"role": "user", "content": alignment_prompt}],
        response_format=StructuralSchemaMapping
    )
    
    with open("field_mapping.json", "w", encoding="utf-8") as f:
        f.write(response.choices[0].message.content)
        
    print("Success: Generated 'field_mapping.json'.")

if __name__ == "__main__":
    run_onboarding()