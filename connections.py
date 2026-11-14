"""Connection factories shared by DataInjection / Query / Update / Delete of every database."""
import psycopg
import pymysql
import redis
from pymongo import MongoClient


def postgres(autocommit=False):
    return psycopg.connect("host=localhost port=5432 dbname=zbd user=admin password=admin", autocommit=autocommit)


def mysql():
    return pymysql.connect(host="127.0.0.1", port=3306, user="root", password="admin", database="zbd")


def mongo():
    return MongoClient("mongodb://admin:admin@localhost:27017", serverSelectionTimeoutMS=5000)["zbd"]


def redis_client():
    return redis.Redis(host="localhost", port=6379, decode_responses=True)
