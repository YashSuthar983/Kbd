import json
import os
import pika
from fastapi import FastAPI
import uvicorn
from sentence_transformers import SentenceTransformer
import chromadb
from notion_client import Client
from datetime import datetime
from typing import Dict, Any, Optional

app = FastAPI(title="Learner Agent")

# Initialize components
print("Initializing Learner Agent components...")

# Load embedding model
embedding_model = SentenceTransformer('all-MiniLM-L6-v2')
print("✓ Loaded sentence transformer model")

# Initialize ChromaDB client
chroma_client = chromadb.HttpClient(host='localhost', port=8000)
collection = chroma_client.get_or_create_collection("incidents")
print("✓ Connected to ChromaDB")

# Initialize Notion client (optional - will work without API key)
notion_api_key = os.environ.get("NOTION_API_KEY")
notion_database_id = os.environ.get("NOTION_DATABASE_ID")
notion = None
if notion_api_key and notion_database_id:
    notion = Client(auth=notion_api_key)
    print("✓ Connected to Notion")
else:
    print("⚠ Notion integration disabled (missing NOTION_API_KEY or NOTION_DATABASE_ID)")

# RabbitMQ setup
connection = pika.BlockingConnection(pika.ConnectionParameters('localhost'))
channel = connection.channel()

# Declare exchange and queue
channel.exchange_declare(exchange='incidents', exchange_type='topic', durable=True)
channel.queue_declare(queue='q.incidents.resolved', durable=True)
channel.queue_bind(exchange='incidents', queue='q.incidents.resolved', routing_key='resolved')

def create_incident_summary(incident_data: Dict[str, Any]) -> str:
    """Creates a text summary for vectorization."""
    # Extract key information from the incident
    incident_id = incident_data.get('id', 'unknown-id')
    title = incident_data.get('title', 'Unknown incident')
    description = incident_data.get('description', 'No description available')
    affected_service = incident_data.get('affected_service', 'Unknown service')
    severity = incident_data.get('severity', 'Unknown severity')
    
    # AI analysis information
    ai_hypothesis = incident_data.get('ai_hypothesis', 'No hypothesis provided')
    confidence_score = incident_data.get('confidence_score', 0.0)
    
    # Resolution information
    resolution_action = incident_data.get('resolution_action', 'No action recorded')
    resolution_notes = incident_data.get('resolution_notes', 'No notes available')
    
    # Create comprehensive summary
    summary = (
        f"Incident {incident_id}: {title} affecting {affected_service} "
        f"(Severity: {severity}). "
        f"Description: {description}. "
        f"AI Analysis: {ai_hypothesis} (Confidence: {confidence_score:.2f}). "
        f"Resolution: {resolution_action}. "
        f"Notes: {resolution_notes}."
    )
    
    return summary

def memorize_incident(incident_data: Dict[str, Any]) -> None:
    """Generates embedding and stores it in ChromaDB."""
    try:
        incident_id = incident_data.get('id', f'unknown-{datetime.now().timestamp()}')
        summary = create_incident_summary(incident_data)
        
        # Create the embedding
        embedding = embedding_model.encode(summary).tolist()
        
        # Prepare metadata
        metadata = {
            "service": incident_data.get('affected_service', 'unknown'),
            "severity": incident_data.get('severity', 'unknown'),
            "timestamp": incident_data.get('timestamp', datetime.now().isoformat()),
            "source": incident_data.get('source', 'unknown')
        }
        
        # Store in ChromaDB
        collection.add(
            embeddings=[embedding],
            documents=[summary],
            metadatas=[metadata],
            ids=[incident_id]
        )
        print(f"✓ Memorized incident {incident_id} in ChromaDB")
        
    except Exception as e:
        print(f"✗ Error memorizing incident: {e}")

def create_notion_post_mortem(incident_data: Dict[str, Any]) -> None:
    """Creates a new page in a Notion database."""
    if not notion or not notion_database_id:
        print("⚠ Notion integration not configured, skipping post-mortem creation")
        return
        
    try:
        incident_id = incident_data.get('id', 'unknown-id')
        title = incident_data.get('title', 'Unknown Incident')
        
        # Create the page content
        children = [
            {
                "object": "block",
                "type": "heading_1",
                "heading_1": {
                    "rich_text": [{"type": "text", "text": {"content": f"Post-Mortem: {title}"}}]
                }
            },
            {
                "object": "block",
                "type": "paragraph",
                "paragraph": {
                    "rich_text": [{"type": "text", "text": {"content": f"Incident ID: {incident_id}"}}]
                }
            },
            {
                "object": "block",
                "type": "heading_2",
                "heading_2": {
                    "rich_text": [{"type": "text", "text": {"content": "Description"}}]
                }
            },
            {
                "object": "block",
                "type": "paragraph",
                "paragraph": {
                    "rich_text": [{"type": "text", "text": {"content": incident_data.get('description', 'No description available')}}]
                }
            },
            {
                "object": "block",
                "type": "heading_2",
                "heading_2": {
                    "rich_text": [{"type": "text", "text": {"content": "AI Analysis"}}]
                }
            },
            {
                "object": "block",
                "type": "paragraph",
                "paragraph": {
                    "rich_text": [{"type": "text", "text": {"content": incident_data.get('ai_hypothesis', 'No hypothesis provided')}}]
                }
            },
            {
                "object": "block",
                "type": "heading_2",
                "heading_2": {
                    "rich_text": [{"type": "text", "text": {"content": "Resolution"}}]
                }
            },
            {
                "object": "block",
                "type": "paragraph",
                "paragraph": {
                    "rich_text": [{"type": "text", "text": {"content": incident_data.get('resolution_action', 'No action recorded')}}]
                }
            }
        ]
        
        # Create the page
        notion.pages.create(
            parent={"database_id": notion_database_id},
            properties={
                "Name": {"title": [{"text": {"content": f"Post-Mortem: {title}"}}]},
                "Incident ID": {"rich_text": [{"text": {"content": incident_id}}]},
                "Severity": {"select": {"name": incident_data.get('severity', 'Unknown')}},
                "Service": {"rich_text": [{"text": {"content": incident_data.get('affected_service', 'Unknown')}}]}
            },
            children=children
        )
        print(f"✓ Created Notion post-mortem for incident {incident_id}")
        
    except Exception as e:
        print(f"✗ Error creating Notion post-mortem: {e}")

def process_resolved_incident(ch, method, properties, body):
    """Process resolved incident and learn from it"""
    try:
        incident_data = json.loads(body)
        incident_id = incident_data.get('id', 'unknown-id')
        
        print(f"📚 Learning from resolved incident: {incident_id}")
        
        # 1. Document the incident in Notion
        create_notion_post_mortem(incident_data)
        
        # 2. Memorize the incident in ChromaDB
        memorize_incident(incident_data)
        
        print(f"✅ Successfully processed incident {incident_id}")
        
        # Acknowledge the message
        ch.basic_ack(delivery_tag=method.delivery_tag)
        
    except Exception as e:
        print(f"✗ Error processing incident: {e}")
        # Still acknowledge to avoid reprocessing
        ch.basic_ack(delivery_tag=method.delivery_tag)

# Start consuming
channel.basic_qos(prefetch_count=1)
channel.basic_consume(queue='q.incidents.resolved', on_message_callback=process_resolved_incident)

@app.get("/")
def root():
    return {"message": "Learner Agent is running", "status": "ready"}

@app.get("/health")
def health():
    return {"status": "healthy", "components": {
        "chromadb": "connected",
        "notion": "connected" if notion else "disabled",
        "embedding_model": "loaded"
    }}

@app.get("/stats")
def stats():
    """Get statistics about stored incidents"""
    try:
        count = collection.count()
        return {"total_incidents": count, "status": "success"}
    except Exception as e:
        return {"error": str(e), "status": "error"}

@app.get("/search/{query}")
def search_incidents(query: str, limit: int = 5):
    """Search for similar incidents using vector similarity"""
    try:
        # Create embedding for the query
        query_embedding = embedding_model.encode(query).tolist()
        
        # Search in ChromaDB
        results = collection.query(
            query_embeddings=[query_embedding],
            n_results=limit
        )
        
        return {
            "query": query,
            "results": results,
            "status": "success"
        }
    except Exception as e:
        return {"error": str(e), "status": "error"}

if __name__ == "__main__":
    print("🚀 Learner Agent: Starting to consume resolved incidents...")
    # Start consuming in background
    import threading
    threading.Thread(target=channel.start_consuming, daemon=True).start()
    
    # Start FastAPI
    uvicorn.run(app, host="0.0.0.0", port=8004)
