import subprocess

def build_and_deploy(project_path, project_name):
    image_name = f"{project_name.lower()}-img"
    container_name = f"app-{project_name.lower()}"
    
   
    network_name = "paas-net" 

    print(f"[*] Building Docker image for '{project_name}'...")
    try:
        
        subprocess.run(["docker", "build", "-t", image_name, project_path], check=True)
        print(f"[+] Image built successfully: {image_name}")

        
        subprocess.run(["docker", "rm", "-f", container_name], stderr=subprocess.DEVNULL, stdout=subprocess.DEVNULL)

        print(f"[*] Starting container for '{project_name}'...")
        
      
        subprocess.run([
            "docker", "run", "-d",
            "--name", container_name,
            "--network", network_name, 
            
            # --- TRAEFIK STARTS HERE ---
            "-l", "traefik.enable=true", # Tell Traefik to include this container in routing
            "-l", f"traefik.http.routers.{project_name}.rule=Host(`{project_name}.localhost`)", # URL routing rule
            "-l", f"traefik.http.services.{project_name}.loadbalancer.server.port=80", # Internal port of the application
            
            
            image_name
        ], check=True)

        print(f"[+] EXCELLENT! Your application is live at: http://{project_name}.localhost:8090")
        return True

    except subprocess.CalledProcessError as e:
        print(f"[-] An error occurred during Docker operation: {e}")
        return False


if __name__ == "__main__":
    test_path = "./workspace/sample-app"
    test_name = "sample-app"
    
    build_and_deploy(test_path, test_name)