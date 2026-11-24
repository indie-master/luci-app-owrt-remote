import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"docker"))
import secure_publication_worker as worker
from secure_live_probe import build_trial
from secure_publication import build_publication
from secure_publication_transaction import PublicationTransaction,sha
from transport_profiles import ProfileError
import host_adapter as adapter


class WorkerTests(unittest.TestCase):
    def test_trial_preserves_old_config_and_uses_public_ingress(self):
        live={"inbounds":[{"tag":"old","port":18443,"settings":{"clients":[]}}],
              "routing":{"rules":[{"type":"field","inboundTag":["old"],"outboundTag":"old-route"}]},
              "outbounds":[{"tag":"old-route","protocol":"freedom"}]}
        profile={"name":"new","transport":"tcp-tls","port":24000,
                 "server_name":"new.example.org",
                 "certificate_file":"/etc/router-orchestrator/tls/cert.pem",
                 "key_file":"/etc/router-orchestrator/tls/key.pem"}
        artifact={"profile":profile,"remote_host":"new.example.org","public_port":443}
        inventory={"mode":"external-stream","public_port":443,"allowed_names":["new.example.org"],
                   "existing_routes":[],"occupied_ports":[],"managed_routes":[],
                   "nginx_config_sha256":"a"*64,"proxy_protocol":True}
        plan=build_publication(live,artifact,inventory,profile_id="new",revision=1)
        final=copy.deepcopy(plan["candidate"])
        trial,client=build_trial(final,profile,plan["tag"],public_port=443,
            dial_host="new.example.org",admin_port=25401,ssh_port=25402,
            admin_target=25403,ssh_target=25404,version="Xray 26.3.27",
            proxy_protocol=True)
        self.assertEqual(trial["inbounds"][0],live["inbounds"][0])
        self.assertEqual(trial["routing"]["rules"][-1],live["routing"]["rules"][0])
        self.assertEqual(trial["outbounds"],live["outbounds"])
        self.assertEqual(len(trial["inbounds"][1]["settings"]["clients"]),2)
        self.assertEqual(final,plan["candidate"])
        self.assertEqual(final["inbounds"][1]["settings"]["clients"],[])
        links=[o for o in client["outbounds"] if o["protocol"]=="vless"]
        self.assertEqual(len(links),2)
        self.assertTrue(all(o["settings"]["port"]==443 and o["settings"]["address"]=="new.example.org" for o in links))

    def test_real_server_key_is_not_returned_and_hydration_is_exact(self):
        live={"inbounds":[{"tag":"ro-secure-test-r1","streamSettings":{
            "realitySettings":{"privateKey":"secret-server-key","serverNames":["example.org"]}}}]}
        public=adapter.redact_server_keys(live)
        self.assertNotIn("secret-server-key",json.dumps(public))
        self.assertEqual(adapter.hydrate_server_keys(live,public),live)
        public["inbounds"][0]["streamSettings"]["realitySettings"]["privateKey"]="attacker-key"
        with self.assertRaises(adapter.Rejected):
            adapter.hydrate_server_keys(live,public)

    def test_temporary_proof_identities_are_not_exposed_to_hub(self):
        live={"inbounds":[{"tag":"ro-secure-test-r1","settings":{"clients":[
            {"email":"pilot-probe-trial@owrt-remote","id":"temporary-secret"},
            {"email":"router@owrt-remote","id":"existing-identity"}]}}]}
        public=adapter.redact_server_keys(live)
        self.assertNotIn("temporary-secret",json.dumps(public))
        self.assertIn("existing-identity",json.dumps(public))
        self.assertEqual(len(live["inbounds"][0]["settings"]["clients"]),2)

    def test_missing_tls_mount_is_a_hard_gate(self):
        item={"Id":"container","Image":"sha256:"+"a"*64,"HostConfig":{"NetworkMode":"host"},
              "State":{"Running":True},"Config":{"Cmd":["run","-config",str(worker.CONFIG)]},
              "Mounts":[{"Type":"bind","Source":str(worker.CONFIG.parent),"Destination":"/etc/xray","RW":False}]}
        with patch.object(worker,"docker",return_value=json.dumps([item]).encode()):
            with self.assertRaisesRegex(ProfileError,"required Xray directory mount missing"):
                worker.identity()
            item["Mounts"].append({"Type":"bind","Source":str(worker.MOUNTED_TLS),
                                   "Destination":str(worker.CONTAINER_TLS),"RW":False})
        with patch.object(worker,"docker",return_value=json.dumps([item]).encode()):
            self.assertEqual(worker.identity()["image"],item["Image"])

    def test_recovery_does_not_touch_other_maintenance(self):
        with tempfile.TemporaryDirectory() as tmp:
            hold=Path(tmp)/"hold"
            hold.write_bytes(b"shadow transport transaction\n")
            with patch.object(worker.life,"TRANSPORT_HOLD",hold),patch.object(worker,"tx") as tx:
                self.assertEqual(worker.recover_locked(),"OTHER_MAINTENANCE")
                tx.assert_not_called()
                self.assertTrue(hold.exists())


class TrialJournalTests(unittest.TestCase):
    def setUp(self):
        t=tempfile.TemporaryDirectory()
        self.addCleanup(t.cleanup)
        root=Path(t.name)
        self.xray=root/"xray"
        self.nginx=root/"nginx"
        self.xray.write_bytes(b"old-xray")
        self.nginx.write_bytes(b"old-map")
        state=root/"state"
        state.mkdir(mode=0o700)
        self.tx=PublicationTransaction(state,self.xray,self.nginx)
        self.activations=[]

    def apply(self,proof,accept=lambda:True):
        return self.tx.apply(candidate_xray=b"final",candidate_nginx=b"new-map",
            expected_xray_sha=sha(b"old-xray"),expected_nginx_sha=sha(b"old-map"),
            validate=lambda a,b:None,activate=lambda:self.activations.append(self.xray.read_bytes()),
            trial_xray=b"trial",trial_accept=proof,accept=accept,verify_baseline=lambda:True)

    def test_success_removes_temporary_credentials_before_commit(self):
        def proof():
            self.assertEqual(self.xray.read_bytes(),b"trial")
            return True
        self.assertEqual(self.apply(proof)["phase"],"ACCEPTED")
        self.assertEqual(self.activations,[b"trial",b"final"])
        self.assertEqual(self.xray.read_bytes(),b"final")

    def test_bad_probe_restores_original(self):
        with self.assertRaisesRegex(ProfileError,"trial acceptance"):
            self.apply(lambda:False)
        self.assertEqual(self.activations,[b"trial",b"old-xray"])
        self.assertEqual(self.nginx.read_bytes(),b"old-map")

    def test_interruption_with_trial_file_is_recoverable(self):
        def crash():
            raise KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt):
            self.apply(crash)
        self.assertEqual(self.xray.read_bytes(),b"trial")
        self.assertEqual(self.tx.recover(activate=lambda:None,verify_baseline=lambda:True)["phase"],"ROLLED_BACK")
        self.assertEqual(self.xray.read_bytes(),b"old-xray")
        self.assertEqual(self.nginx.read_bytes(),b"old-map")

    def test_final_failure_rolls_back_after_probe_removal(self):
        with self.assertRaisesRegex(ProfileError,"runtime acceptance"):
            self.apply(lambda:True,accept=lambda:False)
        self.assertEqual(self.activations,[b"trial",b"final",b"old-xray"])


if __name__=="__main__":
    unittest.main()
