import subprocess
import os
import shutil
import stat

def on_rm_error(func, path, exc_info):
    os.chmod(path, stat.S_IWRITE)
    func(path)

def clone_repo(repo_url, project_name):
    base_dir = "./workspace"
    
    if not os.path.exists(base_dir):
        os.makedirs(base_dir)
    
    project_path = os.path.join(base_dir, project_name)
    
    if os.path.exists(project_path):
        print(f"[*] Old '{project_name}' being deleted (Windows Force)...")
        shutil.rmtree(project_path, onexc=on_rm_error)
        
    print(f"[*]  codes are pulling from '{repo_url}'...")
    
    try:
        subprocess.run(["git", "clone", repo_url, project_path], check=True)
        print(f"[+] Successful '{project_path}'")
        return project_path
    
    except subprocess.CalledProcessError as e:
        print(f"[-] An error occurred during Git Clone.: {e}")
        return None