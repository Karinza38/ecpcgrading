import importlib.resources
import json
from os import environ
import re
import subprocess
import zipfile
from pathlib import Path

import click
import tomlkit
from rich import print
from rich.progress import track

CONFIG_FILE = "grading.toml"
DEFAULT_CONFIG = "nsp2grading", "default_config.toml"

RE_STUDENT_NAME = "(?P<name>[a-z]+)_"


@click.group()
@click.pass_context
def cli(ctx):
    """Grading tool for NSP2 data acquisition course."""
    print()
    config_path = find_config_file()
    if ctx.invoked_subcommand != "init":
        if config_path is None:
            print(
                "[bold red]Configuration file not found. First invoke the 'init' command."
            )
            raise click.Abort()
        else:
            config = read_config(config_path)
            ctx.ensure_object(dict)
            ctx.obj["config"] = config
            ctx.obj["grading_home"] = config_path.parent


@cli.command()
def init():
    """Initialize configuration file for grading."""
    config_path = find_config_file()
    if config_path is not None:
        print(f"Configuration file found. Doing nothing.")
    else:
        print("Creating default config file...")
        default_config = importlib.resources.read_text(*DEFAULT_CONFIG)
        Path(CONFIG_FILE).write_text(default_config)


@cli.command("unpack")
@click.pass_context
def uncompress_submissions(ctx):
    """Uncompress student submissions."""
    config = ctx.obj["config"]
    grading_home = ctx.obj["grading_home"]
    code_dir = grading_home / config["general"]["code_dir"]
    code_dir.mkdir(exist_ok=True)
    submissions_dir = grading_home / config["general"]["submissions_dir"]

    submissions = submissions_dir.glob("*.zip")
    for submission in track(list(submissions), description="Unpacking submissions..."):
        student = re.match(RE_STUDENT_NAME, submission.name).group("name")
        student_dir = code_dir / student
        if not student_dir.is_dir():
            student_dir.mkdir()
            zip_file = zipfile.ZipFile(submission)
            zip_file.extractall(path=student_dir)
            print(f"[blue]Processed {student}.")


@cli.group()
def env():
    """Manage conda environments for all students."""
    pass


@env.command("list")
@click.pass_context
def list_environments(ctx):
    """List all student conda environments."""
    environments = get_all_environments()
    for student in get_students(ctx):
        student_env = make_env_name(student)
        if student_env in environments:
            print(student_env)


@env.command("create")
@click.pass_context
@click.option("-f/", "--force/--no-force", default=False)
def create_environments(ctx, force):
    """Create conda environments for all students."""
    environments = get_all_environments()
    students = get_students(ctx)
    for student in track(students, description="Creating environments..."):
        env_name = make_env_name(student)
        if env_name not in environments or force is True:
            print(f"[blue]Creating {env_name}...")
            try:
                subprocess.run(
                    f"conda create -n {env_name} python=3.9 --yes",
                    shell=True,
                    capture_output=True,
                    check=True,
                )
            except subprocess.CalledProcessError as exc:
                print(f"[bold red]Error creating environment: {exc.stderr.decode()}")
                break


@env.command("remove")
@click.pass_context
def remove_environments(ctx):
    """Remove all existing student environments."""
    environments = get_all_environments()
    students = get_students(ctx)
    for student in track(students, description="Removing environments..."):
        env_name = make_env_name(student)
        if env_name in environments:
            print(f"[blue]Removing {env_name}...")
            try:
                subprocess.run(
                    f"conda env remove -n {env_name}",
                    shell=True,
                    capture_output=True,
                    check=True,
                )
            except subprocess.CalledProcessError as exc:
                print(f"[bold red]Error removing environment: {exc.stderr.decode()}")
                continue


@env.command("install")
@click.pass_context
def install_environments(ctx):
    """Install and import packages for all existing student environments.

    Run `poetry install` to install all necessary packages and run `python -c
    "help('modules')"` to import all packages. This will trigger OS-specific
    malware scanners which will greatly speed up future imports.
    """
    environments = get_all_environments()
    students = get_students(ctx)
    for student in track(students, description="Installing environments..."):
        env_name = make_env_name(student)
        if env_name in environments:
            project_path = find_pyproject_toml(ctx, student)
            if project_path is None:
                print(f"[bold red]Error: pyproject.toml not found for {student}")
                continue
            else:
                directory = project_path.parent
                print(f"[blue]Installing {env_name}...")
                try:
                    subprocess.run(
                        f"conda run -n {env_name} poetry install",
                        shell=True,
                        cwd=directory,
                        capture_output=True,
                        check=True,
                    )
                except subprocess.CalledProcessError as exc:
                    print(
                        f"[bold red]Error installing environment: {exc.stderr.decode()}"
                    )
                    continue
                print(f"[blue]Importing {env_name}...")
                try:
                    subprocess.run(
                        f"""conda run -n {env_name} python -c "help('modules')" """,
                        shell=True,
                        capture_output=True,
                        check=True,
                    )
                except subprocess.CalledProcessError as exc:
                    print(
                        f"[bold red]Error importing environment: {exc.stderr.decode()}"
                    )
                    continue


@cli.group()
def shell():
    """Utility functions for shell integration."""
    pass


@shell.command("start")
@click.pass_context
def start_grading(ctx):
    """Start grading students.

    Jump to the first student's project directory and activate the student's
    environment.
    """
    students = get_students(ctx)
    first_student = students[0]
    project_path = find_pyproject_toml(ctx, first_student).parent
    env = make_env_name(first_student)
    print(f'cd "{project_path}";')
    print(f"conda activate {env};")


@shell.command("next")
@click.pass_context
def grade_next_student(ctx):
    """Start grading the next student.

    Jump to the next student's project directory and activate the student's
    environment.
    """
    config = ctx.obj["config"]
    grading_home = ctx.obj["grading_home"]
    code_dir = grading_home / config["general"]["code_dir"]

    cwd = Path.cwd()
    current_dir = cwd.relative_to(code_dir)
    current_student = current_dir.parts[0]

    students = get_students(ctx)
    idx = students.index(current_student)
    next_student = students[(idx + 1) % len(students)]

    project_path = find_pyproject_toml(ctx, next_student).parent
    env = make_env_name(next_student)
    print(f'cd "{project_path}";')
    print(f"conda activate {env};")


def find_config_file():
    """Search current working directory and its parents for config file."""
    cwd = Path.cwd()
    for directory in [cwd] + list(cwd.parents):
        config_path = directory / CONFIG_FILE
        if config_path.exists():
            return config_path
    return None


def read_config(config_path):
    """Read configuration file.

    Args:
        config_path (pathlib.Path): Location of the TOML configuration file.

    Returns:
        dict: a configuration object.
    """
    if config_path is None:
        return None
    else:
        return tomlkit.parse(config_path.read_text())


def make_env_name(student):
    """Make up an environment name for a student.

    Args:
        student (str): Name of the student.

    Returns:
        str: Name of an environment for the student.
    """
    env_name = f"env_{student}"
    return env_name


def get_all_environments():
    """Get all existing environments.

    Returns:
        list: A list of strings of existing environment names.
    """
    process = subprocess.run("conda env list --json", shell=True, capture_output=True)
    environment_paths = [Path(p) for p in json.loads(process.stdout)["envs"]]
    environments = [p.name for p in environment_paths if p.parent.name == "envs"]
    return environments


def get_students(ctx):
    """Get a list of students.

    Return a list of students that have code directories available. The location
    of the code directory is retrieved from the click.Context object.

    Args:
        ctx (click.Context): The click context object.

    Returns:
        list: A list of strings of student names.
    """
    config = ctx.obj["config"]
    grading_home = ctx.obj["grading_home"]
    code_dir = grading_home / config["general"]["code_dir"]
    students = [p.name for p in code_dir.iterdir() if p.is_dir()]
    return sorted(students)


def find_pyproject_toml(ctx, student):
    """Find the pyproject.toml file in a student environment.

    Args:
        ctx (click.Context): The click context object.
        student (str): The name of the student.

    Returns:
        Path: the path to the pyproject.toml file.
    """
    config = ctx.obj["config"]
    grading_home = ctx.obj["grading_home"]
    code_dir = grading_home / config["general"]["code_dir"]
    try:
        return next((code_dir / student).glob("**/pyproject.toml"))
    except StopIteration:
        return None


if __name__ == "__main__":
    cli()
