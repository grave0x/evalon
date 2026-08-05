import evalon

evalon.init("my-agent")


@evalon.observe
def greet(name: str) -> str:
    return f"Hello, {name}!"


print(greet("Ada"))
