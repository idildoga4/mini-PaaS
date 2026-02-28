from fastapi import FastAPI, BackgroundTasks, WebSocket 
from pydantic import BaseModel
from fastapi.middleware.cors import CORSMiddleware
import asyncio 
import os
from git_manager import clone_repo
from docker_manager import build_and_deploy

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], 
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

class DeployRequest(BaseModel):
    repo_url: str
    project_name: str

def run_pipeline(repo_url: str, project_name: str):
    project_path = clone_repo(repo_url, project_name)
    if project_path:
        build_and_deploy(project_path, project_name)

@app.post("/deploy")
async def deploy_project(req: DeployRequest, background_tasks: BackgroundTasks):
    background_tasks.add_task(run_pipeline, req.repo_url, req.project_name)
    return {"message": f"Deployment started for {req.project_name}! You can watch the logs via WebSocket."}


@app.websocket("/ws/{project_name}")
async def websocket_endpoint(websocket: WebSocket, project_name: str):
    await websocket.accept()
    log_path = f"./workspace/{project_name.lower()}.log"
 
    while not os.path.exists(log_path):
        await asyncio.sleep(0.5)

    with open(log_path, "r", encoding="utf-8") as f:
        while True:
            line = f.readline()
       
            if not line:
                await asyncio.sleep(0.5)
                continue
            
    
            await websocket.send_text(line.strip())
          
            if "SUCCESS!" in line or "error occurred" in line.lower():
                break
                
    await websocket.close()