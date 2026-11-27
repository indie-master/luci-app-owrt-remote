import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"docker"))
import secure_profile_catalog as catalog
import secure_profile_store as store
from secure_publication import build_publication, encode
from secure_publication_commit import binding_for, finalize_publication
from secure_publication_transaction import PublicationTransaction, sha
from transport_profiles import ProfileError


class CommitTests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root=Path(tmp.name)
        self.state=self.root/"transaction"
        self.state.mkdir(mode=0o700)
        self.xray=self.root/"xray.json"
        self.nginx=self.root/"managed.map"
        self.inv=self.root/"ingress.json"
        self.original={"inbounds":[],"routing":{"rules":[]},"outbounds":[]}
        self.xray.write_bytes(encode(self.original))
        self.nginx.write_bytes(b"# empty\n")
        self.inventory={"mode":"external-stream","public_port":443,
            "allowed_names":["new.example.org"],"existing_routes":[],
            "occupied_ports":[443],"managed_routes":[],
            "nginx_config_sha256":"a"*64,"proxy_protocol":True}
        self.inv.write_text(json.dumps(self.inventory))
        self.inv.chmod(0o600)
        self.tx=PublicationTransaction(self.state,self.xray,self.nginx)
        with store.connection(self.root) as db:
            store.save_draft(db,profile={"name":"test","transport":"tcp-tls","port":24001,
                "server_name":"new.example.org",
                "certificate_file":"/etc/router-orchestrator/tls/cert.pem",
                "key_file":"/etc/router-orchestrator/tls/key.pem"},
                label="Test",remote_host="new.example.org",public_port=443)
            artifact,_,_=store.load_artifact(db,"test",1)
        self.plan=build_publication(self.original,artifact,self.inventory,profile_id="test",revision=1)
        self.binding=binding_for(self.plan,self.inventory,profile_id="test",revision=1,
                                 expected_nginx_digest="b"*64)

    def finalize(self, record, *, runtime=True, stamp="b"*64):
        with store.connection(self.root) as db:
            return finalize_publication(record,db=db,inventory_path=self.inv,
                xray_path=self.xray,nginx_path=self.nginx,
                nginx_digest=lambda mode:stamp,verify_runtime=lambda:runtime)

    def apply(self, **kwargs):
        args=dict(candidate_xray=encode(self.plan["candidate"]),
            candidate_nginx=self.plan["fragment"].encode(),
            expected_xray_sha=sha(encode(self.original)),expected_nginx_sha=sha(b"# empty\n"),
            validate=lambda a,b:None,activate=lambda:None,accept=lambda:True,
            verify_baseline=lambda:True,binding=self.binding,finalize=self.finalize)
        args.update(kwargs)
        return self.tx.apply(**args)

    def recover(self):
        return self.tx.recover(activate=lambda:self.fail("must not restart after decision"),
            verify_baseline=lambda:True,finalize=self.finalize)

    def visible(self):
        with store.connection(self.root) as db:
            self.assertEqual(catalog.desired_profiles(db,"netis"),[])
            return catalog.available_profiles(db)

    def test_commits_inventory_then_catalog(self):
        self.assertEqual(self.apply()["phase"],"ACCEPTED")
        self.assertEqual(len(self.visible()),1)
        self.assertEqual(json.loads(self.inv.read_text())["managed_routes"],self.plan["routes"])
        self.assertEqual(self.recover()["phase"],"ACCEPTED")

    def test_crash_between_inventory_and_catalog_retries(self):
        with patch.object(catalog,"publish_verified",side_effect=RuntimeError("interrupted")):
            with self.assertRaises(RuntimeError):
                self.apply()
        self.assertEqual(self.tx._record()["phase"],"FILES_ACCEPTED")
        self.assertEqual(self.visible(),[])
        self.assertEqual(json.loads(self.inv.read_text())["managed_routes"],self.plan["routes"])
        self.assertEqual(self.recover()["phase"],"ACCEPTED")
        self.assertEqual(len(self.visible()),1)

    def test_crash_after_catalog_commit_does_not_restore(self):
        def interrupted(record):
            self.finalize(record)
            raise RuntimeError("interrupted after catalog commit")
        with self.assertRaises(RuntimeError):
            self.apply(finalize=interrupted)
        self.assertEqual(len(self.visible()),1)
        self.assertEqual(self.recover()["phase"],"ACCEPTED")
        self.assertEqual(self.xray.read_bytes(),encode(self.plan["candidate"]))

    def test_failed_runtime_or_changed_nginx_never_publishes(self):
        with self.assertRaisesRegex(ProfileError,"runtime"):
            self.apply(finalize=lambda record:self.finalize(record,runtime=False))
        self.assertEqual(self.visible(),[])
        self.assertEqual(json.loads(self.inv.read_text()),self.inventory)
        with self.assertRaisesRegex(ProfileError,"nginx differs"):
            self.finalize(self.tx._record(),stamp="c"*64)
        self.assertEqual(self.visible(),[])
        self.assertEqual(self.recover()["phase"],"ACCEPTED")

    def test_failed_initial_acceptance_restores_without_visibility(self):
        with self.assertRaisesRegex(ProfileError,"acceptance"):
            self.apply(accept=lambda:False)
        self.assertEqual(self.visible(),[])
        self.assertEqual(self.xray.read_bytes(),encode(self.original))
        self.assertEqual(self.nginx.read_bytes(),b"# empty\n")
        self.assertEqual(self.tx._record()["phase"],"ROLLED_BACK")


if __name__=="__main__":
    unittest.main()
