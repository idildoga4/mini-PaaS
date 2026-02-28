import subprocess
import os

def build_and_deploy(project_path, project_name):
    image_name = f"{project_name.lower()}-img"
    container_name = f"app-{project_name.lower()}"
    network_name = "paas-net"
    
    log_path = f"./workspace/{project_name.lower()}.log"
    
    with open(log_path, "a", encoding="utf-8") as log_file:
        log_file.write(f"[*] Process started for '{project_name}'...\n")
        log_file.flush()
        
        try:
            subprocess.run(
                ["docker", "build", "--progress=plain", "-t", image_name, project_path], 
                check=True, 
                stdout=log_file, 
                stderr=subprocess.STDOUT
            )
            log_file.write(f"[+] Image successfully built: {image_name}\n")
            log_file.flush()

            subprocess.run(["docker", "rm", "-f", container_name], stderr=subprocess.DEVNULL, stdout=subprocess.DEVNULL)

            log_file.write(f"[*] Starting container with Traefik...\n")
            log_file.flush()
            
            subprocess.run([
                "docker", "run", "-d",
                "--name", container_name,
                "--network", network_name,
                "-l", "traefik.enable=true",
                "-l", f"traefik.http.routers.{project_name}.rule=Host(`{project_name}.localhost`)",
                "-l", f"traefik.http.services.{project_name}.loadbalancer.server.port=80",
                image_name
            ], check=True, stdout=log_file, stderr=subprocess.STDOUT)

            log_file.write(f"[+] SUCCESS! Your application is live at: http://{project_name}.localhost:8090\n")
            return True

        except subprocess.CalledProcessError as e:
            log_file.write(f"[-] An error occurred during the Docker process: {e}\n")
            return False

if __name__ == "__main__":
    test_path = "./workspace/sample-app"
    test_name = "sample-app"
    
    build_and_deploy(test_path, test_name)