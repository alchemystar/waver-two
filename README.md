# SpringBoot 外部调用与强依赖接口分析器

这是一个**静态分析脚本**，用于快速识别：

1. 工程中的外部调用（HTTP/Feign/MQ/gRPC/JDBC 常见模式）
2. 工程提供的对外接口（Controller Mapping）
3. 哪些接口对外部依赖是“强依赖”（反向递归判定）

> 说明：这是近似分析器，不执行代码，不保证 100% 语义精确。

## 使用方式

```bash
python3 spring_external_dependency_analyzer.py /path/to/springboot-project -o analysis-report.json
```

## 反向分析逻辑（本次实现）

1. **先找外部依赖 Bean**：识别 RestTemplate/WebClient/FeignClient/MQ/Jdbc 等类型或注解。
2. **找强依赖种子方法**：
   - 方法体里有直接外部调用语句；或
   - 方法调用了外部依赖 Bean（例如 `restTemplate.getForObject(...)`、`xxxFeignClient.query(...)`）。
3. **层层向上递归**：沿调用图反向回溯所有调用方，把 strong 标记逐层传播。
4. **判定 endpoint**：如果接口入口方法在 strong 闭包里，则 `strong_dependency=true`。

## 输出关键字段

- `external_call_sites`：直接外部调用方法清单
- `strong_methods`：被判定为强依赖的方法及原因
- `endpoints`：接口结果
  - `dependency_chain_methods`：接口正向可达方法
  - `external_dependencies`：正向可达的直接外部调用点
  - `strong_dependency`：接口是否命中反向强依赖闭包
  - `strong_dependency_reason`：强依赖原因链

## 当前识别的外部调用模式

- RestTemplate / WebClient / Feign
- OkHttp / Apache HttpClient
- KafkaTemplate / RabbitTemplate / RocketMQTemplate
- gRPC（ManagedChannelBuilder / Stub）
- JdbcTemplate

## 限制

- 对 Java/Kotlin 进行正则+结构化浅解析，不是完整 AST。
- 对复杂动态分派、反射、AOP 切面、跨模块远程依赖无法完全还原。
- 对接口方法重载、泛型擦除、复杂注入方式（如工厂Bean）可能有误差。
