import logging
import configparser
import os
import subprocess
import docker
from git import Repo
import git

# Set up logging
logging.basicConfig(filename='deploy.log', level=logging.INFO)

def nginx_configure(nginx, ip: str, name: list, port: int, ssl: bool):
    """
    Add nginx config.

    Args:
        nginx: Docker container for nginx.
        ip (str): IP address of the container.
        name (str): Server name.
        port (int): Port number.
        ssl (bool): Whether SSL is enabled.
        repo_name (str): Name of the repository.
    """
    nginx_config_no_ssl = f"""
    server {{
        listen 80;
        server_name {name};

        location / {{
            proxy_pass http://{ip}:{port};
            proxy_set_header Host $host;
            proxy_set_header X-Real-IP $remote_addr;
            proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        }}
    }}
    """
    nginx_config_ssl = f'''
   server {{
   listen 443 ssl;
   server_name {name};

   ssl_certificate ssl.crt;
   ssl_certificate_key ssl.key;

   location / {{
       proxy_pass http://http://{ip}:{port};
       proxy_set_header Host $host;
       proxy_set_header X-Real-IP $remote_addr;
       proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
       proxy_ssl_verify off;
       proxy_ssl_session_reuse on;
   }}
   }}
   '''

    if ssl:
        if os.path.exists('ssl.crt') and os.path.exists('ssl.key'):
            nginx.exec_run(
                f"echo '{nginx_config_ssl}' >> /etc/nginx/conf.d/example.conf")
    nginx.exec_run(
        f"echo '{nginx_config_no_ssl}' >> /etc/nginx/conf.d/example.conf")
    nginx.exec_run("service nginx restart")

def get_repo_name():
    """
    Get the repository name.

    Returns:
        str: Repository name.
    """
    try:
        result = subprocess.run(
            ['git', 'rev-parse', '--show-toplevel'], capture_output=True, text=True, check=True)
        repo_path = result.stdout.strip()
        repo_name = repo_path.split('/')[-1]
        return repo_name
    except Exception as e:
        logging.error("Error: %s", e)
        return None

def get_repo_url():
    """
    Get the repository URL.

    Returns:
        str: Repository URL.
    """
    try:
        result = subprocess.run(
            ['git', 'remote', 'get-url', 'origin'], capture_output=True, text=True, check=False)
        repo_url = result.stdout.strip()
        return repo_url
    except Exception as e:
        logging.error("Error: %s", e)
        return None

def clone_or_pull_repo(repo_path, repo_url):
    """
    Clone the repository if it doesn't exist, or fetch the latest changes.

    Args:
        repo_path (str): Path to the repository.
        repo_url (str): URL of the repository.
    """
    if not os.path.exists(repo_path):
        Repo.clone_from(repo_url)
    else:
        repo = Repo(repo_path)
        repo.remotes.origin.pull()

def load_config(config_path):
    """
    Load the config file.

    Args:
        config_path (str): Path to the config file.

    Returns:
        configparser.ConfigParser: Config object.
    """
    if not os.path.exists(config_path):
        logging.error('Config file not found')
        return None
    config = configparser.ConfigParser()
    config.read(config_path)
    return config

def check_docker_daemon():
    """
    Check if the Docker daemon is running.

    Returns:
        bool: True if the Docker daemon is running, False otherwise.
    """
    client = docker.from_env()
    if not client.ping():
        logging.error('Docker daemon is not running')
        return False
    return True

def build_docker_image(repo_path,client):
    """
    Build a Docker image from the Dockerfile in the repo.

    Args:
        repo_path (str): Path to the repository.

    Returns:
        docker.models.images.Image: Docker image object.
    """
    image = client.images.build(path=repo_path)
    return image

def create_network_and_nginx(client):
    """
    Create a Docker network and start an nginx container.

    Args:
        client (docker.client.DockerClient): Docker client object.

    Returns:
        tuple: Network and nginx container objects.
    """
    network = client.networks.get('main')
    if network:
        pass
    else:
        network = client.networks.create('main')
        nginx = client.containers.create(
            image='nginx:latest',
            ports={'80/tcp': 8080,
                   '443/tcp': 8443},
            detach=True
        )
        nginx.start()

        network.connect(nginx)
        nginx.reload()
    return network, nginx

def rename_old_containers(client, repo_name):
    """
    Rename old containers.

    Args:
        client (docker.client.DockerClient): Docker client object.
        repo_name (str): Name of the repository.
    """
    for container in client.containers.list():
        if container.image.tags[0] == repo_name:
            container.rename(repo_name + "_old")

def run_extra_containers(client, config):
    """
    Run extra containers specified in the config.

    Args:
        client (docker.client.DockerClient): Docker client object.
        config (configparser.ConfigParser): Config object.
    """
    for extra in config['pyoku']['extras']:
        keep = config.getboolean(extra, 'keep', fallback=False)
        client.containers.run(
            extra,
            detach=True,
            name=extra + "_" + get_repo_name() + "_keep" if keep else "",
            environment={
                option: value
                for section in config.sections() if section == extra
                for option, value in config.items(section)
            },
        )

def start_new_container(client, image_id, config):
    """
    Start a new container with the new image.

    Args:
        client (docker.client.DockerClient): Docker client object.
        image_id (str): ID of the Docker image.
        config (configparser.ConfigParser): Config object.

    Returns:
        docker.models.containers.Container: Container object.
    """
    container = client.containers.run(
        image_id,
        detach=True,
        environment={
            option: value
            for section in config.sections()
            for option, value in config.items(section)
        }
    )
    return container

def stop_old_containers(client, repo_name):
    """
    Stop old containers.

    Args:
        client (docker.client.DockerClient): Docker client object.
        repo_name (str): Name of the repository.
    """
    for container in client.containers.list():
        if container.image.tags[0] == repo_name + "_old" or repo_name in container.image.tags[0]:
            if '_keep' in container.image.tags[0]:
                pass
            container.stop()

def connect_container_to_network(container, network):
    """
    Connect a container to a network.

    Args:
        container (docker.models.containers.Container): Container object.
        network (docker.models.networks.Network): Network object.
    """
    network.connect(container)

def deploy():
    """
    Deploy the application.
    """
    try:
        # Check if the config file exists
        config_path = 'pyoku.ini'
        config = load_config(config_path)
        if not config:
            return

        # Clone the repository if it doesn't exist, or fetch the latest changes
        repo_path = '/' + get_repo_name()
        repo_url = get_repo_url()
        clone_or_pull_repo(repo_path, repo_url)

        # Check if the Docker daemon is running
        if not check_docker_daemon():
            return

        client = docker.from_env()
        # Build a Docker image from the Dockerfile in the repo
        image = build_docker_image(repo_path, client)

        # Create a Docker network and start an nginx container
        network, nginx = create_network_and_nginx(client)

        # Rename old containers
        rename_old_containers(client, get_repo_name())

        # Run extra containers specified in the config
        run_extra_containers(client, config)

        # Start a new container with the new image
        container = start_new_container(client, image.id, config)

        # Stop old containers
        stop_old_containers(client, get_repo_name())

        # Connect the new container to the network
        connect_container_to_network(container, network)

        # Configure nginx
        nginx_configure(
            nginx, container.attrs['NetworkSettings']['IPAddress'], config['pyoku']['domain'], int(config['pyoku']['port']),
            True if config.getboolean('pyoku', 'ssl', fallback=False) else False
        )

        # Log successful deployment
        logging.info('Deployment successful')

    except git.exc.GitCommandError as e:
        logging.error('Git command error: %s', e)
    except docker.errors.APIError as e:
        logging.error('Docker API error: %s', e)
    except Exception as e:
        logging.error('Deployment failed: %s', e)

def main():
    deploy()

if __name__ == '__main__':
    main()