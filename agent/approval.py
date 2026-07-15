"""审批渠道抽象——人在回路工作流的可插拔接口。

图本身对"审批怎么发生"无感知：写操作节点用 LangGraph interrupt 挂起并抛出
审批请求，由外层驱动（runner）拿一个 Approver 去获取决策、再 Command(resume) 续跑。
这样把"审批渠道"从编排里解耦出来——命令行/网页/飞书只是不同的 Approver 实现，
换渠道不动图。飞书等真集成即在此扩展一个 FeishuApprover（本项目留作可选适配器）。
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass
class ApprovalRequest:
    tool: str  # 待执行的写操作工具名
    args: dict  # 工具参数
    summary: str  # 人类可读的"将要发生什么"


@dataclass
class ApprovalDecision:
    approved: bool
    approver: str = ""  # 审批人标识
    comment: str = ""  # 审批意见（拒绝原因等）


class Approver(ABC):
    @abstractmethod
    async def decide(self, req: ApprovalRequest) -> ApprovalDecision: ...


class AutoApprover(Approver):
    """按策略自动裁决——用于自动化评测与测试。

    默认放行；可传 hold_priorities 拦截高优先级工单的创建（模拟"高危操作需人工"）。
    """

    def __init__(self, approve: bool = True, hold_priorities: tuple[str, ...] = ()) -> None:
        self.approve = approve
        self.hold_priorities = hold_priorities

    async def decide(self, req: ApprovalRequest) -> ApprovalDecision:
        if req.args.get("priority") in self.hold_priorities:
            return ApprovalDecision(False, "auto", f"策略拦截：{req.args['priority']} 需人工审批")
        return ApprovalDecision(self.approve, "auto", "" if self.approve else "策略拒绝")


class ConsoleApprover(Approver):
    """命令行审批：打印请求，读 y/n。演示人在回路最直观的方式。"""

    async def decide(self, req: ApprovalRequest) -> ApprovalDecision:
        print(f"\n  ⚖️  审批请求：{req.summary}")
        print(f"      工具={req.tool} 参数={req.args}")
        ans = input("      批准执行？[y/N] ").strip().lower()
        approved = ans in ("y", "yes")
        return ApprovalDecision(approved, "console", "" if approved else "人工拒绝")
