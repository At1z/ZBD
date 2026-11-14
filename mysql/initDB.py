from docker_utils import run_container


def init():
    run_container(
        name="zbd-mysql",
        image="mysql:9.7.4",
        ports={3306: 3306},
        env={"MYSQL_ROOT_PASSWORD": "admin", "MYSQL_DATABASE": "zbd"},
        health_cmd='mysqladmin ping -h localhost -uroot -padmin',
    )
