from fastapi import FastAPI, BackgroundTasks
from pydantic import BaseModel
import git_manager
import docker_manager
import requests

app = FastAPI(title="PaaS Builder Service", description="CI/CD Automation Engine")

class DeployRequest(BaseModel):
    repo_url: str
    project_name: str

def run_pipeline(repo_url: str, project_name: str):
    print(f"\n[>>>]For  {project_name} pipeline started [>>>]")
    
    project_path = git_manager.clone_repo(repo_url, project_name)
    if not project_path:
        print(f"[-] Pipeline stopped: Git Clone is unsucessful.")
        return

    success = docker_manager.build_and_deploy(project_path, project_name)
    
    if success:
        print(f"[+] Pipeline successful!")
        # İleride buraya 1. kişinin API'sine başarılı olduğuna dair istek (Webhook) atma kodu eklenecek
    else:
        print(f"[-] Pipeline stopped: Docker Build/Run unsucessful.")

@app.post("/deploy")
async def trigger_deploy(request: DeployRequest, background_tasks: BackgroundTasks):
    # DİKKAT: İşlemi arka plana atıyoruz. Çünkü git clone ve docker build 
    # dakikalar sürebilir. Kullanıcıyı web sitesinde bekletemeyiz.
    background_tasks.add_task(run_pipeline, request.repo_url, request.project_name)
   
    return {
        "status": "success",
        "message": f"'{request.project_name}' The project has been added to the queue. The build process has started in the background."
    }