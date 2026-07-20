#!/usr/bin/env python3
# pip3 install pyyaml

# Скрипт выполняет задачу:
# показывает оригинальные Entrypoint/Cmd образов, используемых в compose-шаблонах
# проектов. Нужен для подключения секретного шима compass_secret_shim: при
# переопределении entrypoint в compose оригинальный ENTRYPOINT образа теряется,
# и его необходимо явно зафиксировать в command.
#
# Запускать на сервере с доступом к docker (и скачанными образами):
#     sudo python3 script/show_image_entrypoints.py -e production -v compass
#     sudo python3 script/show_image_entrypoints.py -e production -v compass --check-shell

import sys

sys.dont_write_bytecode = True

import re, json, argparse, subprocess, yaml
from pathlib import Path
from utils import scriptutils

# ---АРГУМЕНТЫ СКРИПТА---#
parser = argparse.ArgumentParser()

parser.add_argument('-v', '--values', required=False, default="compass", type=str, help='Название values файла окружения')
parser.add_argument('-e', '--environment', required=False, default="production", type=str, help='Окружение, в котором развернут проект')
parser.add_argument('-p', '--project', required=False, default="", type=str, help='Проект (по умолчанию — все проекты)')
parser.add_argument('--pull', required=False, action="store_true", help='Скачать отсутствующие образы перед проверкой (docker pull)')
parser.add_argument('--check-shell', required=False, action="store_true", help='Проверить наличие /bin/sh в образе (запускает контейнер)')

args = parser.parse_args()

# ---КОНЕЦ АРГУМЕНТОВ СКРИПТА---#

# ---ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ---#


# достаем значение по точечному пути из values (a.b.c)
def resolve_dotted_path(values_dict: dict, dotted_path: str):
    node = values_dict
    for path_part in dotted_path.split("."):
        if isinstance(node, dict) and path_part in node:
            node = node[path_part]
        else:
            return None

    return node


# собираем все образы из compose-шаблонов проекта
def collect_project_images(project_name: str):
    image_list = []
    for compose_path in sorted(Path(root_path + "/src/" + project_name).glob("compose*.goyaml")):
        for compose_line in compose_path.read_text().splitlines():
            matched = re.match(r'^\s*image:\s*(.+?)\s*$', compose_line)
            if not matched:
                continue
            image_list.append((compose_path.name, matched.group(1).strip('"\'')))

    return image_list


# подставляем значения из values в шаблонные вставки образа
def render_image(image_template: str, values_dict: dict):
    unresolved = []

    def replace_template(match):
        resolved = resolve_dotted_path(values_dict, match.group(1))
        if resolved is None:
            unresolved.append(match.group(0))
            return match.group(0)
        return str(resolved)

    rendered = re.sub(r"\{\{\s*\.([A-Za-z0-9_.]+)\s*\}\}", replace_template, image_template)

    return rendered, unresolved


# ---СКРИПТ---#

scriptutils.assert_root()

script_dir = str(Path(__file__).parent.resolve())
root_path = str(Path(script_dir + "/../").resolve())

values_name = args.values
environment = args.environment

# резолвим values-файл той же цепочкой, что и deploy.py
specified_values_path = None
for candidate_name in [
    "values.%s.%s.yaml" % (environment, values_name),
    "values.%s.yaml" % values_name,
]:
    candidate_path = root_path + "/src/" + candidate_name
    if Path(candidate_path).exists():
        specified_values_path = candidate_path
        break

if specified_values_path is None:
    scriptutils.die("Не найден values файл для -v %s (src/values.%s.yaml)" % (values_name, values_name))

# мерджим дефолты с указанным values (указанный приоритетнее)
with open(root_path + "/src/values.yaml", "r") as defaults_file:
    values_dict = yaml.safe_load(defaults_file)
with open(specified_values_path, "r") as specified_file:
    values_dict = scriptutils.merge(values_dict, yaml.safe_load(specified_file))

# определяем список проектов
if args.project:
    project_list = [args.project]
else:
    project_list = sorted([p.name for p in Path(root_path + "/src").iterdir() if p.is_dir() and (p / "compose.goyaml").exists()])

print(scriptutils.cyan("Образы и их оригинальные Entrypoint/Cmd (для подключения compass_secret_shim)"))
print("values: %s" % specified_values_path)
print("---")

seen_images = {}
for project_name in project_list:
    for compose_file_name, image_template in collect_project_images(project_name):
        image_name, unresolved_parts = render_image(image_template, values_dict)

        if unresolved_parts:
            print(scriptutils.warning(
                "[%s] %s — не удалось разрешить шаблон (%s), проверьте вручную"
                % (project_name, image_template, ", ".join(unresolved_parts))
            ))
            continue

        if image_name in seen_images:
            continue
        seen_images[image_name] = True

        inspect = subprocess.run(
            ["docker", "image", "inspect", "--format", "{{json .Config.Entrypoint}} {{json .Config.Cmd}}", image_name],
            capture_output=True, text=True,
        )

        if inspect.returncode != 0:
            if args.pull:
                subprocess.run(["docker", "pull", image_name])
                inspect = subprocess.run(
                    ["docker", "image", "inspect", "--format", "{{json .Config.Entrypoint}} {{json .Config.Cmd}}", image_name],
                    capture_output=True, text=True,
                )

        if inspect.returncode != 0:
            print(scriptutils.warning("[%s] %s — образ отсутствует на сервере" % (project_name, image_name)))
            continue

        try:
            entrypoint, cmd = json.loads("[" + inspect.stdout.strip().replace("} {", "}, {") + "]")
        except json.JSONDecodeError:
            print(scriptutils.warning("[%s] %s — не удалось разобрать inspect: %s" % (project_name, image_name, inspect.stdout)))
            continue

        print("[%s] %s" % (project_name, image_name))
        print("  Entrypoint: %s" % json.dumps(entrypoint))
        print("  Cmd:        %s" % json.dumps(cmd))

        if not entrypoint:
            print("  => шим:     entrypoint: [\"/bin/sh\", \"/compass_secret_shim.sh\"] (command не нужен)")
        else:
            print("  => шим:     entrypoint: [\"/bin/sh\", \"/compass_secret_shim.sh\"]")
            print("              command: %s" % json.dumps(entrypoint + cmd))

        if args.check_shell:
            shell_check = subprocess.run(
                ["docker", "run", "--rm", "--entrypoint", "/bin/sh", image_name, "-c", "echo shell-ok"],
                capture_output=True, text=True,
            )
            if shell_check.returncode == 0 and "shell-ok" in shell_check.stdout:
                print("  /bin/sh:    " + scriptutils.success("есть"))
            else:
                print("  /bin/sh:    " + scriptutils.error("ОТСУТСТВУЕТ — шим невозможен, оставить пароль в env"))

        print("---")

print("Напоминание: при подключенном шиме оригинальный ENTRYPOINT образа переносится в command.")
print("Перед каждым релизом образов перезапускайте этот скрипт и сверяйте значения.")
