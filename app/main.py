from fastapi import FastAPI

app = FastAPI(title="exotomailcow")


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}
