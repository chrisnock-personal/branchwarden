import os

from jinja2 import Environment, FileSystemLoader

HERE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "templates")


def test_every_template_parses():
    env = Environment(loader=FileSystemLoader(HERE))
    names = env.list_templates()
    assert "pulls.html" in names
    for name in names:
        env.get_template(name)
