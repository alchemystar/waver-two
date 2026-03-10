#!/usr/bin/env python3
"""
Spring Boot 外部调用与对外接口强依赖分析器（静态近似版）

反向分析核心：
1) 先识别“外部依赖 Bean”（如 RestTemplate/WebClient/FeignClient/MQ/Jdbc 等）
2) 找出“直接调用外部依赖 Bean”或“直接外部调用语句”的方法作为强依赖种子
3) 沿调用图反向递归回溯调用方，逐层传播 strong_dependency
4) 对 Controller endpoint 判断其方法是否落入强依赖闭包
"""

from __future__ import annotations

import argparse
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Set, Tuple


REQUEST_ANNOTATIONS = {
    "RequestMapping",
    "GetMapping",
    "PostMapping",
    "PutMapping",
    "DeleteMapping",
    "PatchMapping",
}

EXTERNAL_PATTERNS: List[Tuple[str, str]] = [
    ("resttemplate", r"\bRestTemplate\b|\.getForObject\(|\.postForEntity\(|\.exchange\("),
    ("webclient", r"\bWebClient\b|\.retrieve\("),
    ("feign", r"@FeignClient\b|\bFeign\b"),
    ("okhttp", r"\bOkHttpClient\b|\.newCall\("),
    ("apache_httpclient", r"\bCloseableHttpClient\b|HttpClients\.createDefault\(|\.execute\("),
    ("grpc", r"\bManagedChannelBuilder\b|\.blockingStub\(|\.newStub\("),
    ("kafka", r"\bKafkaTemplate\b|\.send\("),
    ("rabbitmq", r"\bRabbitTemplate\b|\.convertAndSend\("),
    ("rocketmq", r"\bRocketMQTemplate\b|\.syncSend\(|\.asyncSend\("),
    ("jdbc", r"\bJdbcTemplate\b|\.queryForObject\(|\.update\("),
]

EXTERNAL_BEAN_TYPE_PATTERNS: List[Tuple[str, str]] = [
    ("resttemplate", r"^RestTemplate$"),
    ("webclient", r"^WebClient$"),
    ("feign", r"Feign|Client$"),
    ("okhttp", r"OkHttpClient"),
    ("apache_httpclient", r"HttpClient"),
    ("grpc", r"Stub$|Channel"),
    ("kafka", r"KafkaTemplate"),
    ("rabbitmq", r"RabbitTemplate"),
    ("rocketmq", r"RocketMQTemplate"),
    ("jdbc", r"JdbcTemplate"),
]


@dataclass
class MethodInfo:
    method_id: str
    class_name: str
    method_name: str
    file: str
    start_line: int
    body: str
    local_calls: Set[str] = field(default_factory=set)
    field_calls: List[Tuple[str, str]] = field(default_factory=list)
    external_calls: Set[str] = field(default_factory=set)


@dataclass
class ClassInfo:
    class_name: str
    file: str
    is_controller: bool = False
    is_external_bean: bool = False
    external_bean_types: Set[str] = field(default_factory=set)
    base_paths: List[str] = field(default_factory=list)
    fields: Dict[str, str] = field(default_factory=dict)
    methods: Dict[str, MethodInfo] = field(default_factory=dict)


@dataclass
class EndpointInfo:
    endpoint_id: str
    http_methods: List[str]
    paths: List[str]
    method_id: str
    file: str
    line: int


class Analyzer:
    def __init__(self, root: Path):
        self.root = root
        self.classes: Dict[str, ClassInfo] = {}
        self.class_by_simple: Dict[str, str] = {}
        self.endpoints: List[EndpointInfo] = []
        self.graph: Dict[str, Set[str]] = {}
        self.reverse_graph: Dict[str, Set[str]] = {}

    def run(self) -> dict:
        source_files = self._collect_sources()
        for f in source_files:
            self._parse_file(f)
        self._build_graph()
        return self._build_report(source_files)

    def _collect_sources(self) -> List[Path]:
        files: List[Path] = []
        for pattern in ("**/*.java", "**/*.kt"):
            files.extend(self.root.glob(pattern))
        return [f for f in files if "/target/" not in str(f) and "/build/" not in str(f)]

    def _parse_file(self, file_path: Path) -> None:
        text = file_path.read_text(encoding="utf-8", errors="ignore")
        lines = text.splitlines()
        rel = str(file_path.relative_to(self.root))

        decl_match = re.search(r"\b(class|interface)\s+(\w+)", text)
        if not decl_match:
            return
        class_name = decl_match.group(2)
        decl_idx = decl_match.start()
        header_text = text[:decl_idx]

        cls = ClassInfo(class_name=class_name, file=rel)
        cls.is_controller = bool(re.search(r"@(RestController|Controller)\b", text))
        cls.base_paths = self._extract_request_paths(header_text)

        if re.search(r"@FeignClient\b", header_text):
            cls.is_external_bean = True
            cls.external_bean_types.add("feign")

        for bean_type, p in EXTERNAL_BEAN_TYPE_PATTERNS:
            if re.search(p, class_name):
                cls.is_external_bean = True
                cls.external_bean_types.add(bean_type)

        field_regex = re.compile(r"(?:private|protected|public)\s+(?:final\s+)?([A-Z]\w*)\s+(\w+)\s*(?:=|;)")
        for field_match in field_regex.finditer(text):
            ftype, fname = field_match.group(1), field_match.group(2)
            cls.fields[fname] = ftype

        for method in self._extract_methods(text, class_name, rel):
            cls.methods[method.method_name] = method
            if cls.is_controller:
                endpoint_meta = self._extract_endpoint_meta(lines, method.start_line)
                if endpoint_meta:
                    http_methods, paths = endpoint_meta
                    full_paths = self._combine_paths(cls.base_paths, paths)
                    endpoint_id = f"{class_name}#{method.method_name}:{','.join(full_paths)}"
                    self.endpoints.append(
                        EndpointInfo(
                            endpoint_id=endpoint_id,
                            http_methods=http_methods,
                            paths=full_paths,
                            method_id=method.method_id,
                            file=rel,
                            line=method.start_line,
                        )
                    )

        self.classes[class_name] = cls
        self.class_by_simple[class_name] = class_name

    def _extract_methods(self, text: str, class_name: str, rel: str) -> List[MethodInfo]:
        methods: List[MethodInfo] = []
        sig = re.compile(
            r"(?m)^\s*(?:public|private|protected)\s+(?:static\s+)?(?:<[^>]+>\s*)?[\w\<\>\[\],\s?]+\s+(\w+)\s*\(([^)]*)\)\s*\{"
        )
        for m in sig.finditer(text):
            name = m.group(1)
            start = m.start()
            start_line = text.count("\n", 0, start) + 1
            body, _ = self._extract_block(text, m.end() - 1)
            if not body:
                continue
            method_id = f"{class_name}#{name}"
            mi = MethodInfo(method_id=method_id, class_name=class_name, method_name=name, file=rel, start_line=start_line, body=body)
            excluded = {"if", "for", "while", "switch", "catch", "return", "new", "throw", name}
            mi.local_calls = set(re.findall(r"\b(\w+)\s*\(", body)) - excluded
            mi.field_calls = re.findall(r"\b(\w+)\.(\w+)\s*\(", body)
            for ext_name, ext_pat in EXTERNAL_PATTERNS:
                if re.search(ext_pat, body):
                    mi.external_calls.add(ext_name)
            methods.append(mi)
        return methods

    def _extract_block(self, text: str, open_brace_idx: int) -> Tuple[str, int]:
        depth = 0
        i = open_brace_idx
        while i < len(text):
            ch = text[i]
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return text[open_brace_idx:i + 1], i
            i += 1
        return "", open_brace_idx

    def _extract_request_paths(self, snippet: str) -> List[str]:
        paths: List[str] = []
        ann = re.findall(r"@(RequestMapping|GetMapping|PostMapping|PutMapping|DeleteMapping|PatchMapping)\s*\(([^)]*)\)", snippet, flags=re.S)
        for _, arg_text in ann:
            paths.extend(re.findall(r'"([^"]+)"', arg_text))
            for key in ("value", "path"):
                kv = re.search(rf"\b{key}\s*=\s*\{{([^}}]+)\}}", arg_text)
                if kv:
                    paths.extend(re.findall(r'"([^"]+)"', kv.group(1)))
        return sorted(set(paths or [""]))

    def _extract_endpoint_meta(self, lines: List[str], method_line: int) -> Tuple[List[str], List[str]] | None:
        idx = method_line - 2
        ann_lines = []
        while idx >= 0 and lines[idx].strip().startswith("@"):
            ann_lines.append(lines[idx].strip())
            idx -= 1
        if not ann_lines:
            return None
        ann_text = "\n".join(reversed(ann_lines))

        http_methods: Set[str] = set()
        paths: List[str] = []
        for ann_name in REQUEST_ANNOTATIONS:
            for m in re.finditer(rf"@{ann_name}\s*\(([^)]*)\)", ann_text, flags=re.S):
                arg_text = m.group(1)
                if ann_name == "GetMapping":
                    http_methods.add("GET")
                elif ann_name == "PostMapping":
                    http_methods.add("POST")
                elif ann_name == "PutMapping":
                    http_methods.add("PUT")
                elif ann_name == "DeleteMapping":
                    http_methods.add("DELETE")
                elif ann_name == "PatchMapping":
                    http_methods.add("PATCH")

                mm = re.search(r"\bmethod\s*=\s*RequestMethod\.(\w+)", arg_text)
                if mm:
                    http_methods.add(mm.group(1).upper())

                quoted = re.findall(r'"([^"]+)"', arg_text)
                paths.extend(quoted)

            if re.search(rf"@{ann_name}\b(?!\s*\()", ann_text):
                if ann_name == "GetMapping":
                    http_methods.add("GET")
                elif ann_name == "PostMapping":
                    http_methods.add("POST")
                elif ann_name == "PutMapping":
                    http_methods.add("PUT")
                elif ann_name == "DeleteMapping":
                    http_methods.add("DELETE")
                elif ann_name == "PatchMapping":
                    http_methods.add("PATCH")

        if not http_methods and not paths:
            return None
        if not paths:
            paths = [""]
        return sorted(http_methods), sorted(set(paths))

    def _combine_paths(self, base_paths: List[str], method_paths: List[str]) -> List[str]:
        out = []
        for b in (base_paths or [""]):
            for m in (method_paths or [""]):
                path = f"{b.rstrip('/')}/{m.lstrip('/')}".replace("//", "/")
                if not path.startswith("/"):
                    path = "/" + path
                out.append(path)
        return sorted(set(out))

    def _build_graph(self) -> None:
        self.graph = {}
        self.reverse_graph = {}
        for cls in self.classes.values():
            for method in cls.methods.values():
                self.graph.setdefault(method.method_id, set())
                self.reverse_graph.setdefault(method.method_id, set())

                for lc in method.local_calls:
                    if lc in cls.methods:
                        target = f"{cls.class_name}#{lc}"
                        self.graph[method.method_id].add(target)

                for field_name, callee in method.field_calls:
                    ftype = cls.fields.get(field_name)
                    if not ftype:
                        continue
                    target_class = self.class_by_simple.get(ftype)
                    if not target_class:
                        continue
                    target = self.classes[target_class].methods.get(callee)
                    if target:
                        self.graph[method.method_id].add(target.method_id)

        for caller, callees in self.graph.items():
            for callee in callees:
                self.reverse_graph.setdefault(callee, set()).add(caller)

    def _external_bean_types_for(self, class_name: str, field_type: str) -> Set[str]:
        out: Set[str] = set()
        for ext_type, p in EXTERNAL_BEAN_TYPE_PATTERNS:
            if re.search(p, field_type):
                out.add(ext_type)
        target_class = self.classes.get(field_type)
        if target_class and target_class.is_external_bean:
            out.update(target_class.external_bean_types)
        return out

    def _identify_strong_methods(self) -> Tuple[Set[str], Dict[str, List[str]]]:
        seeds: Set[str] = set()
        reasons: Dict[str, List[str]] = {}

        for cls in self.classes.values():
            for method in cls.methods.values():
                if method.external_calls:
                    seeds.add(method.method_id)
                    reasons.setdefault(method.method_id, []).append(
                        f"direct_external_call:{','.join(sorted(method.external_calls))}"
                    )

                for field_name, _ in method.field_calls:
                    ftype = cls.fields.get(field_name)
                    if not ftype:
                        continue
                    bean_types = self._external_bean_types_for(cls.class_name, ftype)
                    if bean_types:
                        seeds.add(method.method_id)
                        reasons.setdefault(method.method_id, []).append(
                            f"calls_external_bean:{field_name}:{ftype}:{','.join(sorted(bean_types))}"
                        )

        strong_methods = set(seeds)
        stack = list(seeds)
        while stack:
            cur = stack.pop()
            for caller in self.reverse_graph.get(cur, set()):
                if caller not in strong_methods:
                    strong_methods.add(caller)
                    reasons.setdefault(caller, []).append(f"calls_strong_method:{cur}")
                    stack.append(caller)

        for mid in reasons:
            reasons[mid] = sorted(set(reasons[mid]))
        return strong_methods, reasons

    def _collect_reachable(self, start: str) -> Set[str]:
        seen: Set[str] = set()
        stack = [start]
        while stack:
            cur = stack.pop()
            if cur in seen:
                continue
            seen.add(cur)
            stack.extend(self.graph.get(cur, set()) - seen)
        return seen

    def _build_report(self, source_files: List[Path]) -> dict:
        strong_methods, strong_reasons = self._identify_strong_methods()

        external_call_sites = []
        for cls in self.classes.values():
            for m in cls.methods.values():
                if m.external_calls:
                    external_call_sites.append(
                        {
                            "method": m.method_id,
                            "file": m.file,
                            "line": m.start_line,
                            "external_types": sorted(m.external_calls),
                        }
                    )

        endpoint_results = []
        for ep in self.endpoints:
            reach = self._collect_reachable(ep.method_id)
            deps = []
            for mid in sorted(reach):
                c, mn = mid.split("#", 1)
                mi = self.classes[c].methods.get(mn)
                if mi and mi.external_calls:
                    deps.append({"method": mid, "external_types": sorted(mi.external_calls), "file": mi.file, "line": mi.start_line})

            is_strong = ep.method_id in strong_methods
            endpoint_results.append(
                {
                    "endpoint_id": ep.endpoint_id,
                    "http_methods": ep.http_methods,
                    "paths": ep.paths,
                    "file": ep.file,
                    "line": ep.line,
                    "dependency_chain_methods": sorted(reach),
                    "external_dependencies": deps,
                    "strong_dependency": is_strong,
                    "strong_dependency_reason": strong_reasons.get(ep.method_id, ["no reverse strong path"]),
                }
            )

        strong_method_details = []
        for mid in sorted(strong_methods):
            c, mn = mid.split("#", 1)
            mi = self.classes.get(c, ClassInfo(c, "")).methods.get(mn) if c in self.classes else None
            strong_method_details.append(
                {
                    "method": mid,
                    "file": mi.file if mi else "",
                    "line": mi.start_line if mi else -1,
                    "reasons": strong_reasons.get(mid, []),
                }
            )

        return {
            "root": str(self.root),
            "source_file_count": len(source_files),
            "class_count": len(self.classes),
            "endpoint_count": len(self.endpoints),
            "external_call_site_count": len(external_call_sites),
            "external_call_sites": external_call_sites,
            "strong_method_count": len(strong_methods),
            "strong_methods": strong_method_details,
            "endpoints": endpoint_results,
            "note": "strong_dependency 采用反向递归规则：先外部依赖Bean/外部调用方法做种子，再沿调用关系向上回溯。",
        }


def main() -> None:
    parser = argparse.ArgumentParser(description="分析 SpringBoot 工程外部调用与接口强依赖")
    parser.add_argument("project_root", help="SpringBoot 项目根目录")
    parser.add_argument("-o", "--output", default="analysis-report.json", help="输出 JSON 路径")
    args = parser.parse_args()

    root = Path(args.project_root).resolve()
    if not root.exists():
        raise SystemExit(f"project_root 不存在: {root}")

    analyzer = Analyzer(root)
    report = analyzer.run()

    out = Path(args.output)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"分析完成: {out} (endpoints={report['endpoint_count']}, external_call_sites={report['external_call_site_count']})")


if __name__ == "__main__":
    main()
