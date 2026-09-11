"""Demo 被测服务 —— 模拟电商 API。

用于端到端演示：API 测试 Agent 生成的用例可以直接打这个服务。
启动：python -m uvicorn examples.demo_api:app --port 8100

接口：
- POST /api/v1/login                登录（校验用户名/密码）
- POST /api/v1/products             创建商品（name 必填、≤100 字符；price > 0）
- GET  /api/v1/products/{id}        查询商品
- PUT  /api/v1/products/{id}        更新商品（price > 0）
- DELETE /api/v1/products/{id}      删除商品
"""
from __future__ import annotations

import time
import uuid

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

app = FastAPI(title="Demo 电商服务", version="1.0.0")

# 模拟用户（账号：admin / 123456）
USERS = {"admin": "123456"}
# 内存商品表
PRODUCTS: dict[int, dict] = {}
_SEQ = [1000]


class LoginReq(BaseModel):
    username: str = Field(..., min_length=1)
    password: str = Field(..., min_length=1)


class ProductReq(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    price: float = Field(..., gt=0, le=999999)


@app.get("/healthz")
def healthz():
    return {"status": "ok"}


@app.post("/api/v1/login")
def login(req: LoginReq):
    time.sleep(0.05)  # 模拟网络延迟
    if USERS.get(req.username) != req.password:
        raise HTTPException(status_code=401, detail="用户名或密码错误")
    return {"token": uuid.uuid4().hex, "user": req.username}


@app.post("/api/v1/products", status_code=201)
def create_product(req: ProductReq):
    _SEQ[0] += 1
    pid = _SEQ[0]
    PRODUCTS[pid] = {"id": pid, "name": req.name, "price": req.price, "created_at": time.time()}
    return PRODUCTS[pid]


@app.get("/api/v1/products/{product_id}")
def get_product(product_id: int):
    p = PRODUCTS.get(product_id)
    if p is None:
        raise HTTPException(status_code=404, detail="商品不存在")
    return p


@app.put("/api/v1/products/{product_id}")
def update_product(product_id: int, req: ProductReq):
    if product_id not in PRODUCTS:
        raise HTTPException(status_code=404, detail="商品不存在")
    PRODUCTS[product_id].update({"name": req.name, "price": req.price})
    return PRODUCTS[product_id]


@app.delete("/api/v1/products/{product_id}", status_code=204)
def delete_product(product_id: int):
    if product_id not in PRODUCTS:
        raise HTTPException(status_code=404, detail="商品不存在")
    del PRODUCTS[product_id]
    return None
