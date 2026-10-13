/*
 * aiPr Link —— ExtendScript 运行时。
 *
 * pymiere 的 Python 侧会生成这样的代码发到 Premiere 执行：
 *
 *   var tmp = $._pymiere['xxx'].name;                       // 通过注册表访问对象
 *   var newPymiereId = $._pymiere.generateId();             // 要一个 id
 *   $._pymiere[newPymiereId] = tmp;                         // 存起来
 *   tmp = ExtendJSON.stringify({...}, internal_variables_replacer, 0, 1);
 *   ... catch(e){ e.error = true; ExtendJSON.stringify(e) }  // 错误也要序列化成 JSON
 *
 * 所以这里必须提供三个全局：$._pymiere、ExtendJSON、internal_variables_replacer。
 * ExtendScript 只支持 ES3 语法，不要用 let/const/箭头函数/模板字符串。
 */

(function () {
    if (typeof $ !== "undefined" && $ !== null && !$._pymiere) {
        var registry = {
            counter: 0,
            generatedIds: [],
            _lastCode: ""
        };

        registry.generateId = function () {
            registry.counter += 1;
            var id = "pm_" + registry.counter + "_" + (new Date()).getTime().toString(36);
            registry.generatedIds[registry.generatedIds.length] = id;
            return id;
        };

        $._pymiere = registry;
    }
})();

if (typeof ExtendJSON === "undefined") {
    ExtendJSON = (function () {
        var ESCAPE_MAP = {
            '"': '\\"',
            "\\": "\\\\",
            "\n": "\\n",
            "\r": "\\r",
            "\t": "\\t",
            "\b": "\\b",
            "\f": "\\f"
        };

        function unicodeEscape(charCode) {
            var hex = charCode.toString(16);
            while (hex.length < 4) {
                hex = "0" + hex;
            }
            return "\\u" + hex;
        }

        function escapeString(text) {
            var result = '"';
            for (var index = 0; index < text.length; index++) {
                var character = text.charAt(index);
                var escaped = ESCAPE_MAP[character];
                if (escaped) {
                    result += escaped;
                } else {
                    var charCode = text.charCodeAt(index);
                    // 非 ASCII（含中文路径/文件名）统一转成 \uXXXX，避免跨进程编码问题
                    result += (charCode < 32 || charCode > 126) ? unicodeEscape(charCode) : character;
                }
            }
            return result + '"';
        }

        function isArray(value) {
            return (value instanceof Array) || (value && value.constructor === Array);
        }

        function isError(value) {
            if (value instanceof Error) {
                return true;
            }
            return !!(value && value.name && value.message);
        }

        function contains(list, value) {
            for (var index = 0; index < list.length; index++) {
                if (list[index] === value) {
                    return true;
                }
            }
            return false;
        }

        function serializeError(errorObject) {
            var source = errorObject.source || ($._pymiere && $._pymiere._lastCode) || "";
            var lines = String(source).split("\n");
            var line = errorObject.line || 1;
            if (line < 1) {
                line = 1;
            }
            // 保证 Python 侧按行取源码时不会越界
            while (lines.length < line) {
                lines[lines.length] = "";
            }
            var message = errorObject.message || errorObject.description || String(errorObject);
            return "{"
                + '"error":true,'
                + '"name":' + escapeString(String(errorObject.name || "ExtendScriptError")) + ","
                + '"message":' + escapeString(String(message)) + ","
                + '"line":' + line + ","
                + '"source":' + escapeString(lines.join("\n"))
                + "}";
        }

        function stringify(value, replacer, space, indent) {
            var replacerFunction = (typeof replacer === "function") ? replacer : null;
            var seen = [];

            function write(holder, key, depth) {
                var current = holder[key];
                if (replacerFunction) {
                    current = replacerFunction.call(holder, key, current);
                    if (current === undefined) {
                        return undefined;
                    }
                }

                if (current === null || current === undefined) {
                    return "null";
                }
                var type = typeof current;
                if (type === "number") {
                    return isFinite(current) ? String(current) : "null";
                }
                if (type === "boolean") {
                    return current ? "true" : "false";
                }
                if (type === "string") {
                    return escapeString(current);
                }
                if (type === "function") {
                    return undefined;
                }
                if (isError(current)) {
                    return serializeError(current);
                }
                if (isArray(current)) {
                    if (contains(seen, current)) {
                        return '"[circular]"';
                    }
                    seen[seen.length] = current;
                    var items = [];
                    for (var index = 0; index < current.length; index++) {
                        var item = write(current, index, depth + 1);
                        items[items.length] = (item === undefined) ? "null" : item;
                    }
                    seen.length = seen.length - 1;
                    return "[" + items.join(",") + "]";
                }
                if (type === "object") {
                    if (contains(seen, current)) {
                        return '"[circular]"';
                    }
                    seen[seen.length] = current;
                    var pairs = [];
                    for (var property in current) {
                        var serialized = write(current, property, depth + 1);
                        if (serialized !== undefined) {
                            pairs[pairs.length] = escapeString(property) + ":" + serialized;
                        }
                    }
                    seen.length = seen.length - 1;
                    return "{" + pairs.join(",") + "}";
                }
                return escapeString(String(current));
            }

            return write({ "": value }, "", 0);
        }

        return {
            stringify: stringify
        };
    })();
}

if (typeof internal_variables_replacer === "undefined") {
    // reflect / 函数等属性会造成循环引用或无法序列化，统一过滤掉
    function internal_variables_replacer(key, value) {
        if (key === "reflect" || key === "__proto__" || key === "constructor") {
            return undefined;
        }
        if (typeof value === "function") {
            return undefined;
        }
        return value;
    }
}
