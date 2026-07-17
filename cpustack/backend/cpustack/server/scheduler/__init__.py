"""调度器包：Filter Chain + Placement Scorer。

调度三阶段流水线：
1. 资源评估：估算模型 CPU/内存需求
2. 候选选择：Filter Chain 过滤
   - StatusFilter：Worker 就绪
   - InstructionSetFilter：指令集匹配（CPU 特有）
   - MemoryFitFilter：内存适配
   - LabelMatchingFilter：标签匹配
3. 放置打分：SPREAD/BINPACK
"""
