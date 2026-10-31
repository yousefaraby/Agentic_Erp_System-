"""Back up the foundation database and verify restore in a disposable database."""
import subprocess
import uuid
from pathlib import Path

def run(*args):
    subprocess.run(["docker", "compose", "-f", str(Path(__file__).with_name("compose.yml")), *args], check=True, timeout=300)

def main():
    token=uuid.uuid4().hex[:12]
    temporary_db="erp_restore_check_"+token
    remote="/tmp/erp_foundation_"+token+".dump"
    output=Path(__file__).parent/"backups"
    output.mkdir(exist_ok=True)
    destination=output/("erp_foundation_"+token+".dump")
    run("exec","-T","db","pg_dump","-U","erp","-d","erp_foundation","-Fc","-f",remote)
    run("cp","db:"+remote,str(destination))
    created=False
    try:
        run("exec","-T","db","createdb","-U","erp",temporary_db)
        created=True
        run("exec","-T","db","pg_restore","-U","erp","--exit-on-error","-d",temporary_db,remote)
        run("run","--rm","-e","ERP_DATABASE_URL=postgresql+psycopg://erp:local-development-only@db:5432/"+temporary_db,
            "app","python","-m","database.integrity")
        print("Backup restored and reconciled successfully:",destination.resolve())
    finally:
        if created:
            run("exec","-T","db","dropdb","-U","erp",temporary_db)
        run("exec","-T","db","rm","-f",remote)

if __name__=="__main__": main()
