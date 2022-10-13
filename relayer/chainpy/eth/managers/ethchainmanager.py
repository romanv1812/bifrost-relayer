import threading
from typing import Optional, Union

from ..ethtype.consts import ChainIndex
from ..ethtype.hexbytes import EthHashBytes, EthAddress, EthHexBytes
from ..ethtype.amount import EthAmount
from ..ethtype.account import EthAccount
from ..ethtype.contract import Abi, EthContract
from ..ethtype.transaction import TransactionType0, TransactionType2
from ..managers.configs import EntityRootConfig
from ..managers.txhandler import EthTxHandler, SendTxUnion
from ..managers.eventhandler import EthEventHandler


class EthChainManager(EthTxHandler, EthEventHandler):
    def __init__(self, chain_index: ChainIndex, root_config: EntityRootConfig):
        super(EthChainManager, self).__init__(chain_index, root_config)

        self.__chain_index = chain_index
        self.__account = EthAccount.from_secret(root_config.entity.secret_hex)
        self.__nonce = self.eth_get_user_nonce(self.__account.address)
        self.__nonce_lock = threading.Lock()

        chain_config = root_config.get_chain_config(chain_index)

        # construct contracts store
        if chain_config.contracts is None:
            chain_config.contracts = []
        self.__contracts_dict = dict()
        for contract_config in chain_config.contracts:
            abi_path = root_config.project_root_path + contract_config.abi_path
            contract_abi = Abi.from_json_file(abi_path)
            contract_obj = EthContract(contract_config.name, contract_config.address, contract_abi)
            self.__contracts_dict[contract_config.name] = contract_obj

    @property
    def account(self) -> EthAccount:
        return self.__account

    @property
    def issue_nonce(self) -> int:
        self.__nonce_lock.acquire()
        nonce = self.__nonce
        self.__nonce += 1
        self.__nonce_lock.release()
        return nonce

    def reset_nonce(self):
        self.__nonce_lock.acquire()
        self.__nonce = self.eth_get_user_nonce(self.__account.address)
        self.__nonce_lock.release()

    def return_nonce(self):
        self.__nonce -= 1

    def get_contract_by_name(self, contract_name: str) -> Optional[EthContract]:
        return self.__contracts_dict.get(contract_name)

    def _get_contract_addr_and_build_tx_data(self, contract_name: str, method_name: str, method_params: list) -> tuple:
        contract = self.get_contract_by_name(contract_name)
        contract_addr = contract.address
        data = contract.abi.get_method(method_name).encode_input_data(method_params)
        return contract_addr, data

    def call_transaction(self, contract_name: str, method_name: str, method_params: list, sender_addr: EthAddress = None) -> Union[EthHexBytes, tuple]:
        contract_addr, data = self._get_contract_addr_and_build_tx_data(
            contract_name,
            method_name,
            method_params
        )

        if sender_addr is None:
            sender_addr = self.__account.address

        call_tx = self.build_call_tx(
            contract_addr,
            data,
            value=None,
            sender_addr=sender_addr
        )

        result = self.call_tx(call_tx)
        contract = self.get_contract_by_name(contract_name)
        return contract.abi.get_method(method_name).decode_output_data(result)

    def build_transaction(self, contract_name: str, method_name: str, method_params: list, value: EthAmount = None) -> Optional[SendTxUnion]:
        contract_addr, data = self._get_contract_addr_and_build_tx_data(
            contract_name,
            method_name,
            method_params
        )
        return self.build_tx_including_fee_upper_bound(
            contract_addr,
            data,
            EthAmount(0) if value is None else value
        )

    def send_transaction(self,
                         tx_with_fee: SendTxUnion,
                         boost: bool = False,
                         resend: bool = False,
                         gas_limit_multiplier: float = 1.0) -> (SendTxUnion, EthHashBytes):
        # estimate tx and setting gas parameter
        is_sendable, pre_processed_tx = self.set_gas_limit_and_fee(
            tx_with_fee,
            self.__account,
            boost=boost,
            resend=resend,
            gas_limit_multiplier=gas_limit_multiplier
        )

        if is_sendable:
            tx_with_fee.nonce = self.issue_nonce
            try:
                tx_hash = self.send_tx(tx_with_fee, self.__account)
            except Exception as e:
                print(str(e))
                tx_hash = EthHashBytes.zero()
        else:
            tx_hash = EthHashBytes.zero()

        return pre_processed_tx, tx_hash

    def resend_transaction(self, tx: SendTxUnion):
        tx.boost_upper_bound()
        tx_hash = self.send_tx(tx, self.__account)
        return tx, tx_hash

    def transfer_native_coin(self,
                             receiver: EthAddress,
                             value: EthAmount,
                             boost: bool = False,
                             resend: bool = False) -> (SendTxUnion, EthHashBytes):
        raw_tx = self.build_tx_including_fee_upper_bound(receiver, EthHexBytes.zero(), value)
        return self.send_transaction(raw_tx, boost=boost, resend=resend)

    def native_balance(self, addr: EthAddress = None) -> EthAmount:
        if addr is None:
            addr = self.account.address
        return self.eth_get_balance(addr)