"""
Piku and Dokku combined in under 350 lines of python
"""
import logging
from typing import Optional, Tuple
import configparser
import os
import subprocess
import docker
from stat import S_IXUSR
from git import Repo
import git
import click
import sys

# Vars
PIKU_RAW_SOURCE_URL = "https://raw.githubusercontent.com/piku/piku/master/piku.py"
PIKU_ROOT = os.environ.get("PIKU_ROOT", os.path.join(os.environ["HOME"], ".piku"))
PIKU_BIN = os.path.join(os.environ["HOME"], "bin")
PIKU_SCRIPT = os.path.realpath(__file__)
PIKU_PLUGIN_ROOT = os.path.abspath(os.path.join(PIKU_ROOT, "plugins"))
APP_ROOT = os.path.abspath(os.path.join(PIKU_ROOT, "apps"))
DATA_ROOT = os.path.abspath(os.path.join(PIKU_ROOT, "data"))
ENV_ROOT = os.path.abspath(os.path.join(PIKU_ROOT, "envs"))
GIT_ROOT = os.path.abspath(os.path.join(PIKU_ROOT, "repos"))
ACME_ROOT = os.environ.get("ACME_ROOT", os.path.join(os.environ["HOME"], ".acme.sh"))
ACME_WWW = os.path.abspath(os.path.join(PIKU_ROOT, "acme"))
ACME_ROOT_CA = os.environ.get("ACME_ROOT_CA", "letsencrypt.org")

# Set up logging
logging.basicConfig(filename="deploy.log", level=logging.INFO)


def sanitize_app_name(app):
    """Sanitize the app name and build matching path"""

    app = (
        "".join(c for c in app if c.isalnum() or c in (".", "_", "-"))
        .rstrip()
        .lstrip("/")
    )
    return app


def nginx_configure(nginx, ip: str, name: str, port: int, ssl: bool) -> None:
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
    nginx_config_ssl = f"""
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
   """

    if ssl:
        if os.path.exists("ssl.crt") and os.path.exists("ssl.key"):
            nginx.exec_run(
                f"echo '{nginx_config_ssl}' >> /etc/nginx/conf.d/example.conf"
            )
    nginx.exec_run(f"echo '{nginx_config_no_ssl}' >> /etc/nginx/conf.d/example.conf")
    nginx.exec_run("service nginx restart")


def get_repo_name() -> str:
    """
    Get the repository name.

    Returns:
        str: Repository name.
    """
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            check=True,
        )
        repo_path = result.stdout.strip()
        repo_name = repo_path.split("/")[-1]
        return repo_name
    except Exception as e:
        logging.error("Error: %s", e)
        return ""


def get_repo_url() -> str:
    """
    Get the repository URL.

    Returns:
        str: Repository URL.
    """
    try:
        result = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            capture_output=True,
            text=True,
            check=False,
        )
        repo_url = result.stdout.strip()
        return repo_url
    except Exception as e:
        logging.error("Error: %s", e)
        return ""


def clone_or_pull_repo(repo_path: str, repo_url: str) -> None:
    """
    Clone the repository if it doesn't exist, or fetch the latest changes.

    Args:
        repo_path (str): Path to the repository.
        repo_url (str): URL of the repository.
    """
    if not os.path.exists(repo_path):
        Repo.clone_from(repo_url, repo_path)
    else:
        repo = Repo(repo_path)
        repo.remotes.origin.pull()


def load_config(config_path: str) -> Optional[configparser.ConfigParser]:
    """
    Load the config file.

    Args:
        config_path (str): Path to the config file.

    Returns:
        configparser.ConfigParser: Config object.
    """
    if not os.path.exists(config_path):
        logging.error("Config file not found")
        return None
    config = configparser.ConfigParser()
    config.read(config_path)
    return config


def check_docker_daemon() -> bool:
    """
    Check if the Docker daemon is running.

    Returns:
        bool: True if the Docker daemon is running, False otherwise.
    """
    client = docker.from_env()
    if not client.ping():
        logging.error("Docker daemon is not running")
        return False
    return True


def build_docker_image(repo_path, client) -> docker.models.images.Image:
    """
    Build a Docker image from the Dockerfile in the repo.

    Args:
        repo_path (str): Path to the repository.

    Returns:
        docker.models.images.Image: Docker image object.
    """
    image, _ = client.images.build(path=repo_path)
    return image


def create_network_and_nginx(client) -> Tuple:
    """
    Create a Docker network and start an nginx container.

    Args:
        client (docker.client.DockerClient): Docker client object.

    Returns:
        tuple: Network and nginx container objects.
    """
    network = client.networks.get("main")
    if network:
        pass
    else:
        network = client.networks.create("main")
        nginx = client.containers.create(
            image="nginx:latest", ports={"80/tcp": 8080, "443/tcp": 8443}, detach=True
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


def run_extra_containers(client, config) -> None:
    """
    Run extra containers specified in the config.

    Args:
        client (docker.client.DockerClient): Docker client object.
        config (configparser.ConfigParser): Config object.
    """
    for extra in config["pyoku"]["extras"]:
        keep = config.getboolean(extra, "keep", fallback=False)
        client.containers.run(
            extra,
            detach=True,
            name=extra + "_" + get_repo_name() + "_keep" if keep else "",
            environment={
                option: value
                for section in config.sections()
                if section == extra
                for option, value in config.items(section)
            },
        )


def start_new_container(client, image_id, config) -> docker.models.containers.Container:
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
        },
    )
    return container


def stop_old_containers(client, repo_name) -> None:
    """
    Stop old containers.

    Args:
        client (docker.client.DockerClient): Docker client object.
        repo_name (str): Name of the repository.
    """
    for container in client.containers.list():
        if (
            container.image.tags[0] == repo_name + "_old"
            or repo_name in container.image.tags[0]
        ):
            if "_keep" in container.image.tags[0]:
                pass
            container.stop()


def connect_container_to_network(container, network) -> None:
    """
    Connect a container to a network.

    Args:
        container (docker.models.containers.Container): Container object.
        network (docker.models.networks.Network): Network object.
    """
    network.connect(container)


@click.command("git-hook")
@click.argument("app")
def cmd_git_hook(app):
    """INTERNAL: Post-receive git hook"""

    app = sanitize_app_name(app)
    repo_path = os.path.join(GIT_ROOT, app)
    app_path = os.path.join(APP_ROOT, app)
    data_path = os.path.join(DATA_ROOT, app)

    for line in sys.stdin:
        # pylint: disable=unused-variable
        oldrev, newrev, refname = line.strip().split(" ")
        # Handle pushes
        if not os.path.exists(app_path):
            click.echo("-----> Creating app '{}'".format(app), fg="green")
            os.makedirs(app_path)
            # The data directory may already exist, since this may be a full redeployment (we never delete data since it may be expensive to recreate)
            if not os.path.exists(data_path):
                os.makedirs(data_path)
            clone_or_pull_repo(app, repo_path)
        deploy(app, newrev=newrev)


@click.command("git-receive-pack")
@click.argument("app")
def cmd_git_receive_pack(app):
    """INTERNAL: Handle git pushes for an app"""

    app = sanitize_app_name(app)
    hook_path = os.path.join(GIT_ROOT, app, "hooks", "post-receive")
    env = globals()
    env.update(locals())

    if not os.exists(hook_path):
        os.makedirs(os.dirname(hook_path))
        # Initialize the repository with a hook to this script
        subprocess.call("git init --quiet --bare " + app, cwd=GIT_ROOT, shell=True)
        with open(hook_path, "w") as h:
            h.write(
                """#!/usr/bin/env bash
set -e; set -o pipefail;
cat | PIKU_ROOT="{PIKU_ROOT:s}" {PIKU_SCRIPT:s} git-hook {app:s}""".format(**env)
            )
        # Make the hook executable by our user
        os.chmod(hook_path, os.stat(hook_path).st_mode | S_IXUSR)
    # Handle the actual receive. We'll be called with 'git-hook' after it happens
    subprocess.call(
        'git-shell -c "{}" '.format(sys.argv[1] + " '{}'".format(app)),
        cwd=GIT_ROOT,
        shell=True,
    )


@click.command("git-upload-pack")
@click.argument("app")
def cmd_git_upload_pack(app):
    """INTERNAL: Handle git upload pack for an app"""
    app = sanitize_app_name(app)
    env = globals()
    env.update(locals())
    # Handle the actual receive. We'll be called with 'git-hook' after it happens
    subprocess.call(
        'git-shell -c "{}" '.format(sys.argv[1] + " '{}'".format(app)),
        cwd=GIT_ROOT,
        shell=True,
    )


def deploy(app: str, newrev=None) -> None:
    """
    Deploy the application.
    """
    try:
        # Clone the repository if it doesn't exist, or fetch the latest changes
        repo_path = os.path.join(APP_ROOT, app)
        env = {"GIT_WORK_DIR": repo_path}
        click.echo("-----> Deploying app '{}'".format(app), fg="green")
        subprocess.call("git fetch --quiet", cwd=repo_path, env=env, shell=True)

        if newrev:
            subprocess.call(
                "git reset --hard {}".format(newrev), cwd=repo_path, env=env, shell=True
            )
        subprocess.call("git submodule init", cwd=repo_path, env=env, shell=True)
        subprocess.call("git submodule update", cwd=repo_path, env=env, shell=True)
        # Check if the config file exists
        config_path = os.path.join(repo_path, "pyoku.ini")
        config = load_config(config_path)
        if not config:
            return

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
            nginx,
            container.attrs["NetworkSettings"]["IPAddress"],
            config["pyoku"]["domain"],
            int(config["pyoku"]["port"]),
            config.getboolean("pyoku", "ssl", fallback=False),
        )

        # Log successful deployment
        click.echo("Deployment successful")

    except git.exc.GitCommandError as e:
        click.echo("Git command error: %s", e)
    except docker.errors.APIError as e:
        click.echo("Docker API error: %s", e)
    except Exception as e:
        click.echo("Deployment failed: %s", e)
