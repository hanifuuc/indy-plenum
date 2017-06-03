from time import perf_counter

import pytest

from plenum.common.constants import DOMAIN_LEDGER_ID, LedgerState
from plenum.common.util import updateNamedTuple
from plenum.test.delayers import cqDelay, cr_delay
from stp_zmq.zstack import KITZStack

from stp_core.loop.eventually import eventually
from plenum.common.types import HA
from stp_core.common.log import getlogger
from plenum.test.helper import sendReqsToNodesAndVerifySuffReplies
from plenum.test.node_catchup.helper import waitNodeDataEquality, \
    check_ledger_state
from plenum.test.pool_transactions.helper import disconnect_node_and_ensure_disconnected
from plenum.test.test_ledger_manager import TestLedgerManager
from plenum.test.test_node import checkNodesConnected, TestNode
from plenum.test import waits

# Do not remove the next import
from plenum.test.node_catchup.conftest import whitelist

logger = getlogger()
txnCount = 5


@pytest.fixture(scope="function", autouse=True)
def limitTestRunningTime():
    # remove general limit for this module
    return None


def testNodeCatchupAfterRestart(newNodeCaughtUp, txnPoolNodeSet, tconf,
                                nodeSetWithNodeAddedAfterSomeTxns,
                                tdirWithPoolTxns, allPluginsPath):
    """
    A node that restarts after some transactions should eventually get the
    transactions which happened while it was down
    :return:
    """
    looper, newNode, client, wallet, _, _ = nodeSetWithNodeAddedAfterSomeTxns
    logger.debug("Stopping node {} with pool ledger size {}".
                 format(newNode, newNode.poolManager.txnSeqNo))
    disconnect_node_and_ensure_disconnected(looper, txnPoolNodeSet, newNode)
    looper.removeProdable(newNode)
    # TODO: Check if the node has really stopped processing requests?
    logger.debug("Sending requests")

    # Here's where we apply some load
    for i in range(50):
        sendReqsToNodesAndVerifySuffReplies(looper, wallet, client, 5)

    logger.debug("Starting the stopped node, {}".format(newNode))
    nodeHa, nodeCHa = HA(*newNode.nodestack.ha), HA(*newNode.clientstack.ha)
    newNode = TestNode(newNode.name, basedirpath=tdirWithPoolTxns, config=tconf,
                       ha=nodeHa, cliha=nodeCHa, pluginPaths=allPluginsPath)
    looper.add(newNode)
    txnPoolNodeSet[-1] = newNode

    # Delay catchup reply processing so LedgerState does not change
    delay_catchup_reply = 5
    newNode.nodeIbStasher.delay(cr_delay(delay_catchup_reply))
    looper.run(checkNodesConnected(txnPoolNodeSet))

    # Make sure ledger starts syncing (sufficient consistency proofs received)
    looper.run(eventually(check_ledger_state, newNode, DOMAIN_LEDGER_ID,
                          LedgerState.syncing, retryWait=.5, timeout=5))

    confused_node = txnPoolNodeSet[0]
    cp = newNode.ledgerManager.ledgerRegistry[DOMAIN_LEDGER_ID].catchUpTill
    start, end = cp.seqNoStart, cp.seqNoEnd
    cons_proof = confused_node.ledgerManager._buildConsistencyProof(
        DOMAIN_LEDGER_ID, start, end)

    bad_send_time = None

    def chk():
        nonlocal bad_send_time
        entries = newNode.ledgerManager.spylog.getAll(
            newNode.ledgerManager.canProcessConsistencyProof.__name__)
        for entry in entries:
            # `canProcessConsistencyProof` should return False after `syncing_time`
            if entry.result == False and entry.starttime > bad_send_time:
                return
        assert False

    def send_and_chk(ledger_state):
        nonlocal bad_send_time, cons_proof
        bad_send_time = perf_counter()
        confused_node.ledgerManager.sendTo(cons_proof, newNode.name)
        # Check that the ConsistencyProof messages rejected
        looper.run(eventually(chk, retryWait=.5, timeout=5))
        check_ledger_state(newNode, DOMAIN_LEDGER_ID, ledger_state)

    send_and_chk(LedgerState.syncing)

    # Not accurate timeout but a conservative one
    timeout = waits.expectedPoolGetReadyTimeout(len(txnPoolNodeSet)) + \
              2*delay_catchup_reply
    waitNodeDataEquality(looper, newNode, *txnPoolNodeSet[:4],
                         customTimeout=timeout)

    send_and_chk(LedgerState.synced)
    # cons_proof = updateNamedTuple(cons_proof, seqNoEnd=cons_proof.seqNoStart,
    #                               seqNoStart=cons_proof.seqNoEnd)
    # send_and_chk(LedgerState.synced)

    sendReqsToNodesAndVerifySuffReplies(looper, wallet, client, 5)
    waitNodeDataEquality(looper, newNode, *txnPoolNodeSet[:4], customTimeout=timeout)

