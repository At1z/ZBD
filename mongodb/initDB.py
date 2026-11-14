from docker_utils import run_container


def init():
    run_container(
        name="zbd-mongodb",
        image="mongo:9.0.8",
        ports={27017: 27017},
        env={"MONGO_INITDB_ROOT_USERNAME": "admin", "MONGO_INITDB_ROOT_PASSWORD": "admin"},
        health_cmd="mongosh --quiet --eval 'db.adminCommand({ping: 1}).ok'",
    )
