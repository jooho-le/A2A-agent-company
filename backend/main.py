from fastapi import FastAPI

app = FastAPI()


@app.get("/")
def read_root():
    return {"message": "A2A backend is running"}
