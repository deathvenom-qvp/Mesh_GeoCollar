CXX ?= g++
CXXFLAGS ?= -std=c++17 -Wall -Wextra -Werror -O1 -g

LIB = ../GeoCollar/CaiXianLin.cpp ../GeoCollar/Fence.cpp ../GeoCollar/Protocol.cpp ../GeoCollar/AppLink.cpp \
      ../GeoCollar/Sha256.cpp
SRC = test_main.cpp $(LIB)

test: geocollar_tests
	./geocollar_tests

geocollar_tests: $(SRC) ../GeoCollar/*.h
	$(CXX) $(CXXFLAGS) -o $@ $(SRC)

# The collar's half of the companion app link, for the app's tests to talk to (app/test).
applink_collar: applink_collar.cpp $(LIB) ../GeoCollar/*.h
	$(CXX) $(CXXFLAGS) -o $@ applink_collar.cpp ../GeoCollar/Protocol.cpp ../GeoCollar/AppLink.cpp ../GeoCollar/Sha256.cpp \
	    ../GeoCollar/Fence.cpp

clean:
	rm -f geocollar_tests applink_collar

.PHONY: test clean
