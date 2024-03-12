import datetime
from enum import StrEnum
import re
from typing import List
from pydantic import BaseModel
from .models import KeyInfo
from ..util import SubprocessSession, SubprocessResult


class PrefCiphers(StrEnum):
    PLAINTEXT = "S0"
    IDEA = "S1"
    TRIPLE_DES = "S2"
    CAST5 = "S3"
    BLOWFISH = "S4"
    AES_128 = "S7"
    AES_192 = "S8"
    AES_256 = "S9"
    TWOFISH = "S10"


class PrefsDigest(StrEnum):
    MD5 = "H1"
    SHA_1 = "H2"
    RIPE_MD = "H3"
    SHA_256 = "H8"
    SHA_384 = "H9"
    SHA_512 = "H10"
    SHA_224 = "H11"


class PrefsCompression(StrEnum):
    UNCOMPRESSED = "Z0"
    ZIP = "Z1"
    ZLIB = "Z2"
    BZIP2 = "Z3"


class AddKeyType(StrEnum):
    DSA = "3"
    RSA_SIGN = "4"
    ELGAMAL = "5"
    RSA_ENCRYPT = "6"
    ECC_SIGN = "10"
    ECC_ENCRYPT = "12"


class AddKeyCurveType(StrEnum):
    CURVE_25519 = "1"
    NIST_P384 = "4"
    BRAINPOOL_P256 = "6"


class KeyListItem(BaseModel):
    type: str
    algorithm: str
    id_hash: str
    id: str
    created: datetime.date | None
    expires: datetime.date | None
    usage: str | None
    trust: str | None
    validity: str | None

    @classmethod
    def from_string(cls, string: str) -> "KeyListItem":
        header, info = string.split("\n", maxsplit=1)
        keytype, algo_and_id = header.strip().split(maxsplit=1)
        algo, key_id = algo_and_id.split("/")
        options = {
            field.strip().split(":")[0].strip(): field.strip().split(":")[1].strip()
            for field in info.strip().replace(": ", ":").replace("\n", " ").split()
        }

        try:
            created = datetime.date.fromisoformat(options["created"])
        except:
            created = None

        try:
            expires = datetime.date.fromisoformat(options["expires"])
        except:
            expires = None

        return KeyListItem(
            type=keytype,
            algorithm=algo,
            id_hash=key_id,
            id=algo_and_id,
            created=created,
            expires=expires,
            usage=options.get("usage"),
            trust=options.get("trust"),
            validity=options.get("validity"),
        )


class UIDListItem(BaseModel):
    status: str
    index: int
    user_id: str
    selected: bool

    @classmethod
    def from_string(cls, string: str) -> "UIDListItem":
        status = re.findall(r"^\[.*\]", string)[0].strip("[]").strip()
        index = re.findall(r"\(.*?\)\.?\*?", string)[0]
        info = string.split(")", maxsplit=1)[1].strip(".*")
        return UIDListItem(
            status=status.strip("[]"), index=int(index.strip("().*")), user_id=info.strip(), selected="*" in index
        )

class RevocationEnum(StrEnum):
    NO_REASON = "0"
    INVALID_UID = "4"

class KeyEditor:
    def __init__(self, key: KeyInfo, session: SubprocessSession, password: str) -> None:
        self.key = key
        self.session = session
        self.process: SubprocessResult = None
        self.password = password

    def check(self):
        if self.process == None or self.process.returncode != None:
            raise RuntimeError("Cannot call function on an inactive Editor")

    def activate(self):
        self.process = self.session.run_command(
            f"gpg --expert --edit-key --command-fd 0 --status-fd 1 --pinentry-mode loopback --batch{f' --passphrase {self.password}' if self.password else ''} {self.key.fingerprint}"
        )
        self.process.wait_for("GET_LINE keyedit.prompt")

    def deactivate(self):
        try:
            self.check()
            self.process.kill()
            self.process = None
        except:
            pass

    def execute(self, command: str, *args: str | None, wait: bool = True) -> str:
        self.check()
        self.process.send(command)
        if len(args) > 0:
            for arg in args:
                self.process.send(arg if arg else "")
        if wait:
            self.process.wait_for("GET_LINE keyedit.prompt")
        return (
            self.process._output.decode()
            .split("[GNUPG:] GET_LINE keyedit.prompt\n")[-2]
            .replace("[GNUPG:] GOT_IT", "")
            .strip()
        )

    def help(self) -> dict[str, str]:
        result = self.execute("help")
        parts = [
            line.split(maxsplit=1)
            for line in result.split("\n")
            if not line[0] in [" ", "*"]
        ]
        return {k: v for k, v in parts}

    def quit(self, save=True) -> None:
        if save:
            self.execute("save")
        else:
            self.execute("quit", "n", "y")

        self.deactivate()

    def list(self) -> tuple[list[KeyListItem], list[UIDListItem]]:
        result = self.execute("list")
        lines = result.split("\n")

        segments: list[list[str]] = []
        for line in lines:
            if re.match(r"^\[.*\].*$", line):
                segments.append(["UID", line])
            elif re.match(r"^\s.*$", line):
                segments[-1][1] += "\n" + line
            else:
                segments.append(["KEY", line])

        keys = [KeyListItem.from_string(seg[1]) for seg in segments if seg[0] == "KEY"]
        users = [UIDListItem.from_string(seg[1]) for seg in segments if seg[0] == "UID"]
        return keys, users

    def select_key(self, id: str | None) -> None:
        self.execute(f"key {id if id else '0'}")

    def select_user_id(self, user_index: int | str | None) -> None:
        self.execute(f"uid {user_index if user_index else '0'}")

    def add_user_id(self, name: str, email: str | None = None, comment: str | None = None) -> None:
        self.execute("adduid", name, email, comment)

    def delete_user_id(self, user_index: int) -> None:
        self.select_user_id(user_index)
        self.execute("deluid", "y")
        self.select_user_id(None)

    def revoke_user_id(
        self,
        user_index: int,
        reason: RevocationEnum = RevocationEnum.INVALID_UID,
        description: str | None = None,
    ) -> None:
        self.select_user_id(user_index)
        self.execute("revuid", "y", reason.value, description, "", "y")
        self.select_user_id(None)

    def set_preferences(
        self,
        user_index: int | str | None,
        cipher: List[PrefCiphers | str] = [PrefCiphers.TRIPLE_DES],
        digest: List[PrefsDigest | str] = [PrefsDigest.SHA_1],
        compression: List[PrefsCompression | str] = [PrefsCompression.UNCOMPRESSED],
        extras: List[str] = [],
    ):
        pref_string = " ".join(
            [
                i
                for i in [
                    " ".join(cipher) if len(cipher) > 0 else None,
                    " ".join(digest) if len(digest) > 0 else None,
                    " ".join(compression) if len(compression) > 0 else None,
                    " ".join(extras) if len(extras) > 0 else None,
                ]
                if i != None
            ]
        )
        self.select_user_id(user_index)
        print(self.execute(f"setpref {pref_string}", "y"))

    def get_preferences(self, user_index: int) -> dict[str, List[str]]:
        self.select_user_id(user_index)
        result = {
            i.strip()
            .split(":")[0]
            .lower(): [x.strip() for x in i.strip().split(":")[1].split(",")]
            for i in self.execute("showpref").split("\n")
            if not i.startswith("[") and len(i.strip()) > 0
        }
        self.select_user_id(None)
        return result

    def add_key(
        self,
        _type: AddKeyType,
        curve: AddKeyCurveType = AddKeyCurveType.CURVE_25519,
        key_length: int = 3072,
        expires: str | int = 0,
    ):
        if _type in [AddKeyType.ECC_SIGN, AddKeyType.ECC_ENCRYPT]:
            result = self.execute(
                "addkey",
                _type.value,
                curve.value,
                expires if type(expires) == str else str(expires),
            )
        else:
            result = self.execute(
                "addkey",
                _type.value,
                str(key_length),
                expires if type(expires) == str else str(expires),
            )
