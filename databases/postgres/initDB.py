from zbd.docker import run_container


def init():
    run_container(
        name="zbd-postgres",
        image="postgres:18.7",
        ports={5432: 5432},
        env={"POSTGRES_USER": "admin", "POSTGRES_PASSWORD": "admin", "POSTGRES_DB": "zbd"},
        health_cmd='pg_isready -U admin -d zbd',
    )
